"""Unit tests for the git-ops subprocess wrapper.

Exercises :mod:`backend.workflow.infrastructure.delivery.git_ops` against a LOCAL bare repository
(``git init --bare`` in ``tmp_path``) standing in for the "remote" — no
network, no real github. Covers the full clone → branch → write → commit →
push round-trip, the no-change ``commit_all`` returning ``False``, and the
token-scrubbing of any logged command / URL.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.workflow.infrastructure.delivery.git_ops import (
    GitOps,
    _strip_https_userinfo,
    scrub_token,
)


async def _run(*args: str, cwd: Path | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return out.decode().strip()


async def _make_bare_remote(tmp_path: Path) -> Path:
    """A bare repo seeded with one initial commit on ``main``."""
    bare = tmp_path / "remote.git"
    await _run("init", "--bare", "-b", "main", str(bare))
    # Seed an initial commit via a throwaway working clone.
    seed = tmp_path / "seed"
    await _run("clone", str(bare), str(seed))
    await _run("config", "user.email", "t@bsvibe.dev", cwd=seed)
    await _run("config", "user.name", "Test", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    await _run("add", "-A", cwd=seed)
    await _run("commit", "-m", "initial", cwd=seed)
    await _run("push", "origin", "main", cwd=seed)
    return bare


async def test_clone_branch_commit_push_roundtrip(tmp_path: Path) -> None:
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    dest = tmp_path / "checkout"

    # A local bare repo is reachable as a file:// URL; no token needed but the
    # token path must still be exercised (scrubbed, never injected into file://).
    await ops.clone(bare.as_uri(), dest, token=None, depth=1)
    assert (dest / "README.md").read_text() == "seed\n"

    await ops.checkout_new_branch(dest, "bsvibe/run-abc123")
    (dest / "answer.txt").write_text("42\n")
    committed = await ops.commit_all(dest, "Add the answer")
    assert committed is True

    await ops.push(dest, "bsvibe/run-abc123", token=None)

    # The bare remote received the branch + commit.
    branches = await _run("branch", "--list", cwd=bare)
    assert "bsvibe/run-abc123" in branches
    log = await _run("log", "bsvibe/run-abc123", "--oneline", cwd=bare)
    assert "Add the answer" in log


async def test_commit_all_no_changes_returns_false(tmp_path: Path) -> None:
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    dest = tmp_path / "checkout"
    await ops.clone(bare.as_uri(), dest, token=None, depth=1)
    await ops.checkout_new_branch(dest, "bsvibe/run-empty")

    # No file edits since the clone → nothing to commit.
    committed = await ops.commit_all(dest, "Nothing here")
    assert committed is False


async def test_commit_all_excludes_verification_byproducts(tmp_path: Path) -> None:
    """Build caches + the verifier's _bsvibe_* acceptance scaffold must NOT land
    in the delivered commit/PR — only the agent's source changes."""
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    dest = tmp_path / "checkout"
    await ops.clone(bare.as_uri(), dest, token=None, depth=1)
    await ops.checkout_new_branch(dest, "bsvibe/run-byp")

    (dest / "mathx.py").write_text("def clamp(v):\n    return v\n")
    (dest / "__pycache__").mkdir(exist_ok=True)
    (dest / "__pycache__" / "mathx.cpython-311.pyc").write_bytes(b"\x00")
    (dest / "tests").mkdir(exist_ok=True)
    (dest / "tests" / "_bsvibe_independent_acceptance.py").write_text("def test_x():\n    pass\n")
    (dest / "tests" / "__pycache__").mkdir(exist_ok=True)
    (dest / "tests" / "__pycache__" / "test_x.pyc").write_bytes(b"\x00")

    committed = await ops.commit_all(dest, "work: clamp")
    assert committed is True

    files = await _run("show", "--name-only", "--pretty=format:", "HEAD", cwd=dest)
    assert "mathx.py" in files
    assert ".pyc" not in files
    assert "__pycache__" not in files
    assert "_bsvibe_independent_acceptance" not in files


async def test_commit_all_noop_when_only_byproducts(tmp_path: Path) -> None:
    """A round that produced ONLY byproducts must not commit (no empty PR)."""
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    dest = tmp_path / "checkout"
    await ops.clone(bare.as_uri(), dest, token=None, depth=1)
    await ops.checkout_new_branch(dest, "bsvibe/run-byp2")

    (dest / "__pycache__").mkdir(exist_ok=True)
    (dest / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\x00")
    (dest / "tests").mkdir(exist_ok=True)
    (dest / "tests" / "_bsvibe_independent_acceptance.py").write_text("def test_x():\n    pass\n")

    committed = await ops.commit_all(dest, "byproducts only")
    assert committed is False


async def test_is_ahead_of_base_true_when_branch_has_extra_commit(tmp_path: Path) -> None:
    """Lift E41 — when the verifier's W2 step already committed the agent's
    edits before delivery runs, ``commit_all`` returns ``False`` (working tree
    clean) BUT the branch still has commits ahead of ``base_branch`` that
    must be pushed + PR'd. The dogfood (run 5a695eb8, 2026-06-17) caught
    this: ``github_delivery_no_changes_noop`` fired even though the agent's
    commit was sitting on HEAD ready to ship.
    """
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    dest = tmp_path / "checkout"
    await ops.clone(bare.as_uri(), dest, token=None, depth=1)
    await ops.checkout_new_branch(dest, "bsvibe/run-ahead")
    # Simulate the verifier's W2 commit_worktree: file added + committed.
    (dest / "feature.txt").write_text("the feature\n")
    await ops.commit_all(dest, "feat: add feature")

    assert await ops.is_ahead_of_base(dest, "main") is True


async def test_is_ahead_of_base_false_when_branch_matches_base(tmp_path: Path) -> None:
    """Lift E41 — a freshly-checked-out branch with no commits beyond
    ``base_branch`` reports ``False``. This is the legitimate no-op
    scenario the existing ``test_github_no_file_changes_no_push_no_pr_clean_success``
    guards: nothing to PR.
    """
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    dest = tmp_path / "checkout"
    await ops.clone(bare.as_uri(), dest, token=None, depth=1)
    await ops.checkout_new_branch(dest, "bsvibe/run-clean")

    assert await ops.is_ahead_of_base(dest, "main") is False


def test_scrub_token_redacts_token_in_url() -> None:
    token = "ghp_supersecrettoken"
    url = f"https://x-access-token:{token}@github.com/owner/repo.git"
    scrubbed = scrub_token(url, token)
    assert token not in scrubbed
    assert "***" in scrubbed
    # The host/path survive so logs stay useful.
    assert "github.com/owner/repo.git" in scrubbed


def test_scrub_token_noop_when_token_none() -> None:
    url = "https://github.com/owner/repo.git"
    assert scrub_token(url, None) == url


def test_authed_url_embeds_token() -> None:
    ops = GitOps()
    authed = ops.authed_url("https://github.com/owner/repo.git", token="abc123")
    assert authed == "https://x-access-token:abc123@github.com/owner/repo.git"
    # No token → unchanged (file:// / local-remote path).
    assert ops.authed_url("file:///tmp/remote.git", token=None) == "file:///tmp/remote.git"


def test_authed_url_idempotent_on_already_authed_url() -> None:
    """Lift E43 — ``GitOps.push`` reads ``origin`` (which may already
    carry the clone-time ``x-access-token:<token>@`` userinfo) and
    re-runs ``authed_url`` on it before pushing. The dogfood retrace
    (run 5a695eb8, 2026-06-17) caught the double-embed:
    ``https://x-access-token:***@x-access-token:***@github.com/…``
    → ``URL rejected: Port number was not a decimal number …``.
    ``authed_url`` must strip any existing userinfo segment before
    embedding the current token so a re-auth produces a SINGLE
    userinfo segment regardless of how many times it is applied.
    """
    ops = GitOps()
    once = ops.authed_url("https://github.com/owner/repo.git", token="ghp_one")
    twice = ops.authed_url(once, token="ghp_two")
    # Exactly ONE userinfo segment in the final URL.
    assert twice.count("x-access-token:") == 1
    # The newest token wins.
    assert "ghp_two" in twice
    assert "ghp_one" not in twice
    # Standard shape: https://x-access-token:<token>@github.com/owner/repo.git
    assert twice == "https://x-access-token:ghp_two@github.com/owner/repo.git"


def test_authed_url_strips_doubled_userinfo() -> None:
    """Lift E44 — `GitOps.push` re-runs `authed_url` on the current
    origin URL, which on a workspace that pre-dates E42 may carry a
    pre-encoded token AND was already token-set at clone time so the
    URL has stacked userinfo (`x-access-token:T@x-access-token:T@host`).
    E43 only stripped the FIRST `@` so the second pass left
    `x-access-token:NEW@x-access-token:T@host` — still doubled. E44
    rsplits on the LAST `@` so any number of stacked userinfo segments
    collapse to a single one in front of the host.
    """
    ops = GitOps()
    doubled = "https://x-access-token:OLD@x-access-token:OLD@github.com/owner/repo.git"
    fixed = ops.authed_url(doubled, token="NEW")
    assert fixed == "https://x-access-token:NEW@github.com/owner/repo.git"
    assert fixed.count("x-access-token:") == 1
    assert "OLD" not in fixed


def test_authed_url_percent_encodes_token_special_chars() -> None:
    """Lift E42 — OAuth-issued tokens (Connect with GitHub via the OAuth
    App flow) can carry URL-reserved characters like ``:`` / ``/`` / ``@``
    / ``?`` / ``#`` / ``%``. The pre-E42 code embedded the token raw, so a
    ``:`` in the token confused git's URL parser into reading it as a
    ``host:port`` split → ``URL rejected: Port number was not a decimal
    number between 0 and 65535``. The E41 dogfood retrace caught this:
    the push fired with the W2 branch ahead of base, but the URL parser
    rejected the embedded token. Fix percent-encodes every reserved char
    so any opaque token shape survives the round-trip.
    """
    ops = GitOps()
    # Token mixes ``:`` (the parser-confusing char) + ``/`` + ``%``.
    raw = "gho_a:b/c%d"
    authed = ops.authed_url("https://github.com/owner/repo.git", token=raw)
    # The raw character sequence MUST NOT appear in the userinfo segment —
    # it should be percent-encoded.
    assert raw not in authed
    # The encoded form must produce a valid userinfo segment that the git
    # client / curl will accept (single ``:`` between user and pass, then
    # ``@`` before the host).
    assert authed.startswith("https://x-access-token:")
    assert "@github.com/owner/repo.git" in authed
    # The encoded representation contains the expected percent-escapes.
    assert "%3A" in authed  # ':'
    assert "%2F" in authed  # '/'
    assert "%25" in authed  # '%'


def test_strip_https_userinfo() -> None:
    # A token-embedded clone URL → clean, credential-free URL.
    assert (
        _strip_https_userinfo("https://x-access-token:ghp_secret@github.com/o/r.git")
        == "https://github.com/o/r.git"
    )
    # Already-clean + non-https URLs are unchanged.
    assert _strip_https_userinfo("https://github.com/o/r.git") == "https://github.com/o/r.git"
    assert _strip_https_userinfo("file:///tmp/x.git") == "file:///tmp/x.git"
    # Stacked userinfo (a pre-E42 clone that was later re-auth'd) collapses to host.
    assert (
        _strip_https_userinfo("https://u:p@x-access-token:tok@github.com/o/r")
        == "https://github.com/o/r"
    )


async def test_scrub_origin_token_removes_pat_from_config(tmp_path: Path) -> None:
    """SECURITY (found via the 2026-07-02 L-measure trace): a token-authed clone
    leaves the PAT in ``.git/config``. ``scrub_origin_token`` must rewrite origin
    to a credential-free URL so no live token persists on disk."""
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    dest = tmp_path / "checkout"
    await ops.clone(bare.as_uri(), dest, token=None, depth=1)
    # Simulate the state a real ``git clone https://x-access-token:PAT@…`` leaves.
    await _run(
        "remote",
        "set-url",
        "origin",
        "https://x-access-token:ghp_SECRET@github.com/o/r.git",
        cwd=dest,
    )

    await ops.scrub_origin_token(dest)

    origin = await _run("remote", "get-url", "origin", cwd=dest)
    assert origin == "https://github.com/o/r.git"
    assert "ghp_SECRET" not in (dest / ".git" / "config").read_text()


async def test_scrub_origin_token_noop_for_clean_origin(tmp_path: Path) -> None:
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    dest = tmp_path / "checkout"
    await ops.clone(bare.as_uri(), dest, token=None, depth=1)
    before = await _run("remote", "get-url", "origin", cwd=dest)
    await ops.scrub_origin_token(dest)
    assert await _run("remote", "get-url", "origin", cwd=dest) == before
