"""Unit tests for the git-ops subprocess wrapper.

Exercises :mod:`backend.workflow.infrastructure.delivery.git_ops` against a LOCAL bare repository
(``git init --bare`` in ``tmp_path``) standing in for the "remote" — no
network, no real github. Covers the full clone → branch → write → commit →
push round-trip, the no-change ``commit_all`` returning ``False``, and the
token-scrubbing of any logged command / URL.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from backend.workflow.infrastructure.delivery.git_ops import (
    GitError,
    GitMergeResult,
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


async def _advance_main(bare: Path, tmp_path: Path, *, path: str, content: str, msg: str) -> None:
    """Push a new commit onto ``main`` in the bare remote via a throwaway clone."""
    work = tmp_path / f"advance-{msg.replace(' ', '_')}"
    await _run("clone", str(bare), str(work))
    await _run("config", "user.email", "t@bsvibe.dev", cwd=work)
    await _run("config", "user.name", "Test", cwd=work)
    (work / path).write_text(content)
    await _run("add", "-A", cwd=work)
    await _run("commit", "-m", msg, cwd=work)
    await _run("push", "origin", "main", cwd=work)


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


async def test_push_scrubs_the_token_from_origin_afterward(tmp_path: Path) -> None:
    """SECURITY — ``push`` re-embeds the token into ``origin`` to authenticate,
    but must scrub it back so a live credential never persists in ``.git/config``
    on disk (the clone-time scrub missed this: a run that DELIVERED left its token
    in the verify-sandbox origin). The scrub must happen even when the push itself
    fails (the token was embedded before the push ran)."""
    dest = tmp_path / "checkout"
    await _run("init", "-b", "main", str(dest))
    await _run("config", "user.email", "t@bsvibe.dev", cwd=dest)
    await _run("config", "user.name", "T", cwd=dest)
    # An https remote that will NOT be reachable — the push fails, but the token
    # scrub must still run (via the push's finally).
    await _run("remote", "add", "origin", "https://github.com/owner/repo.git", cwd=dest)
    (dest / "f.txt").write_text("x\n")
    await _run("add", "-A", cwd=dest)
    await _run("commit", "-m", "c", cwd=dest)

    ops = GitOps()
    token = "ghp_fakeSecretTokenABC123"  # noqa: S105 — test literal, not a real cred
    with contextlib.suppress(GitError):  # the push to the fake remote fails — expected
        await ops.push(dest, "main", token=token)

    origin = await _run("remote", "get-url", "origin", cwd=dest)
    assert token not in origin
    assert "x-access-token" not in origin
    assert origin == "https://github.com/owner/repo.git"


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


# --- PR5: fetch (+ unshallow) + merge_ref ---------------------------------


async def test_merge_ref_clean_merges_non_overlapping_base_change(tmp_path: Path) -> None:
    """PR5 — a run branch B forked from main; main then advances on a
    NON-overlapping file. ``fetch(unshallow=True)`` + ``merge_ref(origin/main)``
    → ``GitMergeResult("clean")`` and HEAD carries BOTH changes."""
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    dest = tmp_path / "checkout"
    await ops.clone(bare.as_uri(), dest, token=None, depth=1)
    await ops.checkout_new_branch(dest, "bsvibe/run-clean-merge")
    (dest / "branch.txt").write_text("from branch\n")
    await ops.commit_all(dest, "feat: branch change")

    # main advances on a different file after the branch forked.
    await _advance_main(bare, tmp_path, path="base.txt", content="from base\n", msg="base advance")

    await ops.fetch(dest, "origin", "main", unshallow=True)
    result = await ops.merge_ref(dest, "origin/main")

    assert result == GitMergeResult(status="clean")
    assert result.conflict_paths == []
    # HEAD now contains both the branch change and the merged-in base change.
    files = await _run("ls-tree", "-r", "--name-only", "HEAD", cwd=dest)
    assert "branch.txt" in files
    assert "base.txt" in files


async def test_merge_ref_conflict_reports_overlapping_path(tmp_path: Path) -> None:
    """PR5 — main advances the SAME lines the branch changed →
    ``GitMergeResult("conflict", [path])``; the tree is left mid-merge (PR6
    decides abort — PR5 only reports)."""
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    dest = tmp_path / "checkout"
    await ops.clone(bare.as_uri(), dest, token=None, depth=1)
    await ops.checkout_new_branch(dest, "bsvibe/run-conflict")
    (dest / "shared.txt").write_text("branch version\n")
    await ops.commit_all(dest, "feat: branch edits shared")

    # main advances the SAME file → the merge cannot auto-reconcile.
    await _advance_main(
        bare, tmp_path, path="shared.txt", content="base version\n", msg="base edits shared"
    )

    await ops.fetch(dest, "origin", "main", unshallow=True)
    result = await ops.merge_ref(dest, "origin/main")

    assert result.status == "conflict"
    assert result.conflict_paths == ["shared.txt"]
    # The working tree is left in a merge state (MERGE_HEAD present).
    assert (dest / ".git" / "MERGE_HEAD").exists()


async def test_fetch_unshallow_enables_merge_base_on_shallow_clone(tmp_path: Path) -> None:
    """PR5 — a ``depth=1`` clone whose ``origin/main`` was updated by a
    depth-maintaining shallow fetch has NO merge base with the run branch, so
    ``merge_ref`` raises ("refusing to merge unrelated histories"). After
    ``fetch(unshallow=True)`` the full history is present and ``merge_ref``
    succeeds.

    The disconnected pre-state is set up with an explicit ``git fetch --depth 1``
    — that is what the network transport does when it maintains the clone's
    shallow depth (over the local ``file://`` transport a plain fetch would
    auto-connect down to the boundary, masking the production hazard PR6 must
    guard against; the ``--depth 1`` fetch reproduces it deterministically).
    """
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    dest = tmp_path / "checkout"
    await ops.clone(bare.as_uri(), dest, token=None, depth=1)
    assert await ops.is_shallow(dest) is True

    await ops.checkout_new_branch(dest, "bsvibe/run-shallow")
    (dest / "branch.txt").write_text("from branch\n")
    await ops.commit_all(dest, "feat: branch change")

    # main advances by several commits after the branch forked.
    for n in (1, 2, 3):
        await _advance_main(
            bare, tmp_path, path="base.txt", content=f"base line {n}\n", msg=f"base advance {n}"
        )

    # A depth-maintaining shallow fetch leaves origin/main as a disconnected
    # graft — no common ancestor with the branch → merge base missing.
    await _run("fetch", "--depth", "1", "origin", "main", cwd=dest)
    assert await ops.is_shallow(dest) is True
    with pytest.raises(GitError):
        await ops.merge_ref(dest, "origin/main")
    # "unrelated histories" is refused BEFORE any merge state is created, so
    # there is nothing to abort — the tree is already clean for the retry.
    assert not (dest / ".git" / "MERGE_HEAD").exists()

    # Unshallowing fetches the full history → the merge base now exists.
    await ops.fetch(dest, "origin", "main", unshallow=True)
    assert await ops.is_shallow(dest) is False
    result = await ops.merge_ref(dest, "origin/main")
    assert result.status == "clean"


async def test_fetch_unshallow_is_noop_on_already_full_clone(tmp_path: Path) -> None:
    """PR5 — ``fetch(unshallow=True)`` on an ALREADY-complete clone is a safe
    no-op (git would error "--unshallow on a complete repository does not make
    sense"; we only pass the flag when the repo is shallow)."""
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    dest = tmp_path / "checkout"
    # depth=0 → a full (non-shallow) clone.
    await ops.clone(bare.as_uri(), dest, token=None, depth=0)
    assert await ops.is_shallow(dest) is False

    # Must not raise despite unshallow=True on a complete repo.
    await ops.fetch(dest, "origin", "main", unshallow=True)
    assert await ops.is_shallow(dest) is False


async def test_checkout_existing_remote_branch_after_full_reclone(tmp_path: Path) -> None:
    """PR6 — the freshness re-clone path: a run branch pushed to the remote is
    checked out on a FRESH full clone via git DWIM tracking (``git checkout
    <branch>`` creates a local tracking branch off ``origin/<branch>``)."""
    bare = await _make_bare_remote(tmp_path)
    ops = GitOps()
    # Push a run branch to the remote via a throwaway clone.
    seed = tmp_path / "seed-branch"
    await ops.clone(bare.as_uri(), seed, token=None, depth=0)
    await ops.checkout_new_branch(seed, "bsvibe/run-reclone")
    (seed / "runfile.txt").write_text("from run\n")
    await ops.commit_all(seed, "feat: run change")
    await ops.push(seed, "bsvibe/run-reclone", token=None)

    # A brand-new full clone lands on main; checkout switches HEAD to the branch.
    dest = tmp_path / "reclone"
    await ops.clone(bare.as_uri(), dest, token=None, depth=0)
    assert not (dest / "runfile.txt").exists()  # main has no run change
    await ops.checkout(dest, "bsvibe/run-reclone")
    assert (dest / "runfile.txt").read_text() == "from run\n"
    branch = await _run("rev-parse", "--abbrev-ref", "HEAD", cwd=dest)
    assert branch == "bsvibe/run-reclone"
