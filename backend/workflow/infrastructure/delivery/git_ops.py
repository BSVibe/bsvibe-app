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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)

_REDACTED = "***"

#: Verification byproducts un-staged from a delivery commit so they never reach
#: the PR: build caches (``__pycache__`` / ``*.pyc`` / tool caches / a stray
#: ``.venv``) and the ``_bsvibe_*`` independent-acceptance scaffold the verifier
#: writes into the workspace. Git glob pathspecs (``**`` = any depth). Mirrors
#: ``backend.storage.product_workspace._COMMIT_EXCLUDE_PATHSPECS`` (the G8 path).
_COMMIT_EXCLUDE_PATHSPECS = (
    ":(glob)**/__pycache__/**",
    ":(glob)**/*.py[co]",
    ":(glob)**/.pytest_cache/**",
    ":(glob)**/.ruff_cache/**",
    ":(glob)**/.mypy_cache/**",
    ":(glob)**/.venv/**",
    ":(glob)**/_bsvibe_*",
)


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


def _strip_https_userinfo(url: str) -> str:
    """Drop any ``<userinfo>@`` credential segment from an ``https://`` URL,
    returning a clean ``https://host/path`` (unchanged for non-https URLs or
    URLs with no userinfo). Mirrors :meth:`GitOps.authed_url`'s ``@`` handling:
    the LAST ``@`` is the userinfo/host boundary (E42 percent-encodes tokens so
    they cannot contain a raw ``@``)."""
    if not url.startswith("https://"):
        return url
    rest = url[len("https://") :]
    if "@" not in rest:
        return url
    return "https://" + rest.rsplit("@", 1)[1]


class GitError(RuntimeError):
    """A ``git`` subprocess exited non-zero. The message is token-scrubbed."""


@dataclass(frozen=True)
class GitMergeResult:
    """Result of a :meth:`GitOps.merge_ref` attempt.

    ``status`` is ``"clean"`` when git completed the merge without conflicts
    (HEAD now points at the merged state) and ``"conflict"`` when at least one
    path conflicts (the working tree is left mid-merge with conflict markers —
    the caller decides whether to abort or resolve). Intentionally a parallel
    local type to ``backend.storage.product_workspace.MergeOutcome``: the
    infrastructure/delivery layer stays decoupled from the storage layer.
    """

    status: Literal["clean", "conflict"]
    conflict_paths: list[str] = field(default_factory=list)


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
        # SECURITY — the clone URL embedded the token, so git wrote
        # ``https://x-access-token:<PAT>@github.com/…`` into ``dest/.git/config``.
        # Leaving it there persists a live credential on disk in the run's verify
        # sandbox (found via the 2026-07-02 L-measure trace). ``push`` re-embeds
        # the token per-operation (it rewrites origin from the fresh token), so we
        # scrub the on-disk URL back to a clean, credential-free form immediately.
        await self.scrub_origin_token(dest)
        # Set a stable committer identity so commit_all never fails on a runner
        # with no global git identity configured.
        await self._run_checked("config", "user.email", "agent@bsvibe.dev", cwd=dest)
        await self._run_checked("config", "user.name", "BSVibe Agent", cwd=dest)

    async def scrub_origin_token(self, dest: Path) -> None:
        """Rewrite ``origin`` to a credential-free URL (never persist a PAT in
        ``.git/config``). A no-op when origin has no embedded userinfo or isn't
        an ``https://`` remote. Safe: :meth:`push` re-authenticates per push."""
        origin = (await self._run_checked("remote", "get-url", "origin", cwd=dest)).strip()
        clean = _strip_https_userinfo(origin)
        if clean != origin:
            await self._run_checked("remote", "set-url", "origin", clean, cwd=dest)

    async def checkout_new_branch(self, dest: Path, branch: str) -> None:
        """Create + switch to a new ``branch`` in the ``dest`` checkout."""
        await self._run_checked("checkout", "-b", branch, cwd=dest)

    async def checkout(self, dest: Path, branch: str) -> None:
        """Switch to an EXISTING ``branch`` in ``dest`` (git DWIM tracking).

        On a FULL clone every remote branch is present as ``origin/<branch>``, so
        ``git checkout <branch>`` auto-creates a local tracking branch off it.
        Used by the freshness re-clone path (PR6): after re-cloning a reaped run
        workspace full-depth, HEAD sits on the default base branch — this moves it
        onto the PR's ``bsvibe/run-<id>`` branch so the base can be merged into it.
        """
        await self._run_checked("checkout", branch, cwd=dest)

    async def commit_all(self, dest: Path, message: str) -> bool:
        """``git add -A`` + commit. Returns ``False`` when nothing changed.

        A run whose work produced no file edits (e.g. a non-code deliverable in
        a github workspace) has an empty ``git status`` → no commit, no PR. The
        caller treats ``False`` as a clean no-op (no empty PR is opened).
        """
        await self._run_checked("add", "-A", cwd=dest)
        # Drop verification byproducts (build caches + the verifier's _bsvibe_*
        # acceptance scaffold) so the delivered PR carries only the agent's
        # source. ``reset`` un-stages them; a pathspec matching nothing is a
        # benign no-op (``_run`` doesn't raise on a non-zero exit).
        await self._run("reset", "-q", "--", *_COMMIT_EXCLUDE_PATHSPECS, cwd=dest)
        # `git diff --cached --quiet` exits 1 when there ARE staged changes. The
        # STAGED set already excludes the byproducts unstaged above, so a round
        # that produced only byproducts is correctly a no-op (no empty PR).
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
        try:
            await self._run_checked(
                "push", "--set-upstream", "origin", branch, cwd=dest, token=token
            )
        finally:
            # SECURITY — the push re-embedded the token into ``origin``; scrub it
            # back so a live credential never persists in ``.git/config`` on disk.
            # The clone-time scrub (:meth:`clone`) missed this: a run that
            # DELIVERED left its token in the verify-sandbox origin. ``finally`` so
            # a failed push still scrubs (the token was embedded before the push).
            if token:
                await self.scrub_origin_token(dest)

    async def is_shallow(self, dest: Path) -> bool:
        """True iff ``dest`` is a shallow clone (``git rev-parse
        --is-shallow-repository`` → ``"true"``).

        A ``--depth 1`` clone (the provisioner's fast-setup default) is shallow;
        an unshallowed / full clone is not. Used to guard ``--unshallow`` so a
        caller can always pass ``unshallow=True`` safely — git errors
        "--unshallow on a complete repository does not make sense", so we only
        pass the flag when the repo is actually shallow.
        """
        code, out, _err = await self._run("rev-parse", "--is-shallow-repository", cwd=dest)
        return code == 0 and out.strip() == "true"

    async def fetch(
        self,
        dest: Path,
        remote: str,
        ref: str,
        *,
        token: str | None = None,
        unshallow: bool = False,
    ) -> None:
        """``git fetch [--unshallow] <remote> <ref>`` inside ``dest``.

        ``remote`` is typically ``"origin"`` (already token-embedded at clone
        time via :meth:`authed_url`), ``ref`` the base branch (e.g. ``"main"``).

        THE SHALLOW MERGE-BASE PROBLEM — a ``--depth 1`` clone can fetch
        ``main``'s tip but lacks the common ancestor, so a later
        :meth:`merge_ref` fails with "refusing to merge unrelated histories"
        (no merge base). ``unshallow=True`` fixes this by fetching the full
        history. To keep a caller able to ALWAYS pass ``unshallow=True`` safely,
        we only add ``--unshallow`` when :meth:`is_shallow` is True — on an
        already-complete clone it is a plain no-op fetch (git would otherwise
        error "--unshallow on a complete repository does not make sense").

        Token handling mirrors :meth:`push`/:meth:`clone`: the origin URL is
        usually already token-embedded so a plain ``git fetch origin <ref>``
        needs no token, but an explicit ``token`` re-authenticates ``origin``
        for THIS fetch only and is scrubbed back afterward. A failed fetch is a
        real error (``_run_checked``). The token NEVER reaches a log line.
        """
        if token:
            origin = await self._run_checked("remote", "get-url", "origin", cwd=dest)
            authed = self.authed_url(origin.strip(), token=token)
            await self._run_checked("remote", "set-url", "origin", authed, cwd=dest, token=token)
        try:
            args = ["fetch"]
            if unshallow and await self.is_shallow(dest):
                args.append("--unshallow")
            args += [remote, ref]
            await self._run_checked(*args, cwd=dest, token=token)
        finally:
            # SECURITY — mirror push: an explicit token was re-embedded into
            # ``origin`` for this fetch; scrub it back so no live credential
            # persists in ``.git/config``. ``finally`` so a failed fetch still
            # scrubs (the token was embedded before the fetch ran).
            if token:
                await self.scrub_origin_token(dest)

    async def merge_ref(self, dest: Path, ref: str) -> GitMergeResult:
        """``git merge --no-ff --no-edit <ref>`` inside ``dest``, reporting conflicts.

        ``ref`` is the fetched remote ref, e.g. ``"origin/main"`` (PR6 will
        :meth:`fetch` with ``unshallow=True`` then ``merge_ref(clone,
        "origin/main")``). Mirrors
        ``backend.storage.product_workspace.merge_main_into_worktree``.

        Exit 0 → :class:`GitMergeResult` ``"clean"`` (HEAD now merged). Exit 1
        (conflicts) → parse the unmerged paths via ``git diff --name-only
        --diff-filter=U`` and return ``"conflict"`` with those paths, leaving the
        tree mid-merge (the caller — PR6 — decides whether to abort). Any OTHER
        exit code (e.g. 128 "refusing to merge unrelated histories" on a shallow
        clone with no merge base) is NOT a merge result and raises.
        """
        # ``--no-ff`` forces a merge commit even when the ref hasn't moved,
        # keeping the run-branch history honest with an explicit merge anchor.
        code, _out, err = await self._run("merge", "--no-ff", "--no-edit", ref, cwd=dest)
        if code == 0:
            return GitMergeResult(status="clean")
        if code == 1:
            # git exits 1 on conflicts — capture the unmerged paths.
            unmerged_code, unmerged_out, _uerr = await self._run(
                "diff", "--name-only", "--diff-filter=U", cwd=dest
            )
            if unmerged_code == 0:
                paths = [p for p in unmerged_out.splitlines() if p.strip()]
                logger.info("merge_ref_conflict", ref=ref, conflict_paths=paths)
                return GitMergeResult(status="conflict", conflict_paths=paths)
        # Any other exit code (e.g. 128 on no merge base / FS errors) is not a
        # merge result — surface it as an error.
        raise GitError(f"git merge {ref} exited {code}: {err.strip()}")


__all__ = ["GitError", "GitMergeResult", "GitOps", "scrub_token"]
