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

from backend.workflow.infrastructure.delivery.git_ops import GitOps, scrub_token


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
