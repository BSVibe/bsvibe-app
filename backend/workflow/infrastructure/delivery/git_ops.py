"""Thin async wrapper over the ``git`` CLI for repo-native delivery.

The github connector is the one delivery target that needs a real DIFF: a PR
is opened against a branch the agent actually committed file edits onto. That
means a run whose delivery target is github must WORK INSIDE a clone of the
target repo (not the empty scratch ``<run_workspace_root>/<run_id>/`` dir), then
commit + push the branch + open the PR.

This module is the small set of git operations that flow needs, each a wrapper
over :func:`asyncio.create_subprocess_exec` (NEVER ``shell=True`` —
python-security #5). Token authentication is via the URL form
``https://x-access-token:<token>@github.com/<owner>/<repo>.git`` (the github
recommended pattern for a PAT / OAuth access token over HTTPS).

**The token is a secret and is NEVER logged.** Every logged command / URL is
passed through :func:`scrub_token`, which replaces the token with ``***``
(python-security #2/#3). The token only ever appears in the argv handed to the
``git`` subprocess, never in a structlog event.

Tested against a LOCAL bare repo (``git init --bare`` in a tmp dir) as the
"remote" — no network, no real github (see ``tests/delivery/test_git_ops.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_REDACTED = "***"


def scrub_token(text: str, token: str | None) -> str:
    """Replace ``token`` with ``***`` in ``text`` so secrets never reach logs.

    A no-op when ``token`` is falsy (e.g. a local ``file://`` remote needs no
    auth). The redaction is a plain substring replace — the token is the
    genuinely secret material, and the surrounding host/path stays intact so the
    log line is still useful for debugging.
    """
    if not token:
        return text
    return text.replace(token, _REDACTED)


class GitError(RuntimeError):
    """A ``git`` subprocess exited non-zero. The message is token-scrubbed."""


class GitOps:
    """Async git operations over the ``git`` CLI (no ``shell=True``)."""

    def __init__(self, *, git_bin: str = "git") -> None:
        self._git = git_bin

    @staticmethod
    def authed_url(repo_url: str, *, token: str | None) -> str:
        """Embed ``token`` into an ``https://`` git URL for push/clone auth.

        github accepts ``https://x-access-token:<token>@github.com/...`` for a
        PAT / OAuth access token. A falsy token (or a non-https remote, e.g. a
        local ``file://`` bare repo used in tests) is returned unchanged.

        Lift E42 — the token is percent-encoded with ``safe=''`` so URL-
        reserved characters (``:`` / ``/`` / ``@`` / ``?`` / ``#`` / ``%``)
        in OAuth-issued tokens don't confuse the URL parser. The E41 dogfood
        caught this: an OAuth token containing ``:`` was read as a
        ``host:port`` split and curl rejected with "Port number was not a
        decimal number between 0 and 65535". PATs (``ghp_…``) only use
        ``[A-Za-z0-9_]`` so they round-tripped unchanged before this fix.
        """
        from urllib.parse import quote  # noqa: PLC0415

        if not token or not repo_url.startswith("https://"):
            return repo_url
        rest = repo_url[len("https://") :]
        # Lift E43 + E44 — strip ALL existing ``<userinfo>@`` segments so
        # a re-auth (e.g. ``push`` reads the origin URL that was already
        # token-embedded at clone time) produces a SINGLE userinfo
        # segment. ``rsplit("@", 1)`` keeps the last ``@`` as the
        # userinfo/host boundary and discards everything before — so
        # both a clean ``user:pass@host`` AND a stacked
        # ``user:pass@user:pass@host`` (from a pre-E42 clone that
        # later got re-auth'd) both collapse to the host alone.
        # E42 percent-encoded tokens cannot contain raw ``@``, so the
        # last ``@`` is unambiguously the userinfo terminator.
        if "@" in rest:
            rest = rest.rsplit("@", 1)[1]
        return f"https://x-access-token:{quote(token, safe='')}@{rest}"

    async def _run(
        self, *args: str, cwd: Path | None = None, token: str | None = None
    ) -> tuple[int, str, str]:
        """Run ``git <args>`` and return ``(returncode, stdout, stderr)``.

        The logged command is token-scrubbed; the token (when present in an
        ``authed_url`` arg) is passed only in the actual argv to the subprocess.
        """
        scrubbed = " ".join(scrub_token(a, token) for a in args)
        logger.info("git_ops_exec", command=f"git {scrubbed}", cwd=str(cwd) if cwd else None)
        proc = await asyncio.create_subprocess_exec(
            self._git,
            *args,
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
        return proc.returncode or 0, out_b.decode(errors="replace"), err_b.decode(errors="replace")

    async def _run_checked(
        self, *args: str, cwd: Path | None = None, token: str | None = None
    ) -> str:
        code, out, err = await self._run(*args, cwd=cwd, token=token)
        if code != 0:
            raise GitError(scrub_token(f"git {' '.join(args)} failed: {err.strip()}", token))
        return out

    async def clone(self, repo_url: str, dest: Path, *, token: str | None, depth: int = 1) -> None:
        """Clone ``repo_url`` into ``dest`` (shallow by default).

        The token is embedded into the clone URL only (never logged). ``dest``
        must not already exist as a non-empty repo — the caller provisions a
        fresh per-run workspace dir.
        """
        url = self.authed_url(repo_url, token=token)
        args = ["clone"]
        if depth and depth > 0:
            args += ["--depth", str(depth)]
        args += [url, str(dest)]
        await self._run_checked(*args, token=token)
        # Set a stable committer identity so commit_all never fails on a runner
        # with no global git identity configured.
        await self._run_checked("config", "user.email", "agent@bsvibe.dev", cwd=dest)
        await self._run_checked("config", "user.name", "BSVibe Agent", cwd=dest)

    async def checkout_new_branch(self, dest: Path, branch: str) -> None:
        """Create + switch to a new ``branch`` in the ``dest`` checkout."""
        await self._run_checked("checkout", "-b", branch, cwd=dest)

    async def commit_all(self, dest: Path, message: str) -> bool:
        """``git add -A`` + commit. Returns ``False`` when nothing changed.

        A run whose work produced no file edits (e.g. a non-code deliverable in
        a github workspace) has an empty ``git status`` → no commit, no PR. The
        caller treats ``False`` as a clean no-op (no empty PR is opened).
        """
        await self._run_checked("add", "-A", cwd=dest)
        # `git diff --cached --quiet` exits 1 when there ARE staged changes.
        code, _out, _err = await self._run("diff", "--cached", "--quiet", cwd=dest)
        if code == 0:
            return False
        await self._run_checked("commit", "-m", message, cwd=dest)
        return True

    async def is_ahead_of_base(self, dest: Path, base_branch: str) -> bool:
        """Lift E41 — True iff HEAD has commits NOT in ``origin/<base_branch>``.

        Used by :func:`deliver_github` to decide "should we still push +
        open the PR even though ``commit_all`` made no new commit?". The
        verifier's W2 ``commit_worktree`` step may have already committed
        the agent's edits before delivery runs; without this check the
        deliver path treats a clean working tree as nothing-to-ship and
        the run rots in ``review_ready``.
        """
        code, out, _err = await self._run(
            "rev-list",
            "--count",
            f"origin/{base_branch}..HEAD",
            cwd=dest,
        )
        if code != 0:
            return False
        try:
            return int(out.strip() or "0") > 0
        except ValueError:
            return False

    async def push(self, dest: Path, branch: str, *, token: str | None) -> None:
        """Push ``branch`` to ``origin``.

        The push re-authenticates by rewriting ``origin`` to the token-embedded
        URL for THIS push only — the clone-time remote URL may have carried the
        token, but re-setting it keeps the auth explicit per operation and
        token-scrubbed in logs.
        """
        if token:
            origin = await self._run_checked("remote", "get-url", "origin", cwd=dest)
            authed = self.authed_url(origin.strip(), token=token)
            await self._run_checked("remote", "set-url", "origin", authed, cwd=dest, token=token)
        await self._run_checked("push", "--set-upstream", "origin", branch, cwd=dest, token=token)


__all__ = ["GitError", "GitOps", "scrub_token"]
