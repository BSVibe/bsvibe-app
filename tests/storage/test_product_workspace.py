"""W1 — git-backed product workspace + per-run worktree lifecycle.

Drives the real subprocess-git module against ``tmp_path`` (no PG / no
sandbox needed). Verifies the FS+git layer at the boundary where the
rest of BSVibe relies on it:

* product workspace init is idempotent
* worktree add returns a checkout of ``main`` on a ``bsvibe/run/<rid>``
  branch
* worktree remove cleans up FS + branch
* the git author identity ends up on commits (so a host with no global
  git config doesn't break things)
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.config import get_settings
from backend.storage.product_workspace import (
    ProductWorkspaceError,
    add_run_worktree,
    init_product_workspace,
    list_product_tree,
    product_workspace_path,
    remove_run_worktree,
    run_branch_name,
    run_worktree_path,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolate_workspace_roots(tmp_path, monkeypatch):
    """Point both workspace roots at ``tmp_path`` so each test gets its own
    FS scratch and no state leaks between tests."""
    monkeypatch.setattr(
        get_settings(),
        "product_workspace_root",
        str(tmp_path / "products"),
        raising=False,
    )
    monkeypatch.setattr(
        get_settings(),
        "run_workspace_root",
        str(tmp_path / "runs"),
        raising=False,
    )


async def _git(*args: str, cwd) -> str:
    """Helper for assertions — runs git and returns stdout."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, f"git {args} failed: {err.decode()}"
    return out.decode().strip()


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------


async def test_paths_are_absolute_even_when_settings_use_relative_root(
    tmp_path, monkeypatch
) -> None:
    """W2 hotfix regression: in prod the settings carry a RELATIVE
    workspace root (``"var/products"``). uvloop's subprocess transport
    raises a bare ``FileNotFoundError`` when ``cwd`` is a relative path
    pointing at a freshly-mkdir'd dir — even when the dir is visible to
    ``mkdir`` and ``ls``. Forcing absolute resolution at the path-builder
    layer keeps git invocations bulletproof across event loops."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(get_settings(), "product_workspace_root", "var/products", raising=False)
    monkeypatch.setattr(get_settings(), "run_workspace_root", "var/runs", raising=False)

    p = product_workspace_path(uuid.uuid4())
    r = run_worktree_path(uuid.uuid4())
    assert p.is_absolute(), f"product path must be absolute: {p}"
    assert r.is_absolute(), f"run worktree path must be absolute: {r}"


# ---------------------------------------------------------------------------
# init_product_workspace
# ---------------------------------------------------------------------------


async def test_init_creates_git_repo_with_initial_commit() -> None:
    product_id = uuid.uuid4()
    await init_product_workspace(product_id)

    path = product_workspace_path(product_id)
    assert (path / ".git").is_dir(), "workspace must be a real git repo (not a worktree)"
    assert (path / ".bsvibe" / "PRODUCT.md").is_file()

    # Initial commit on main exists.
    branch = await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)
    assert branch == "main"
    commit_count = await _git("rev-list", "--count", "HEAD", cwd=path)
    assert int(commit_count) == 1


async def test_init_is_idempotent() -> None:
    product_id = uuid.uuid4()
    await init_product_workspace(product_id)
    first_sha = await _git("rev-parse", "HEAD", cwd=product_workspace_path(product_id))

    await init_product_workspace(product_id)  # second call — must be no-op
    second_sha = await _git("rev-parse", "HEAD", cwd=product_workspace_path(product_id))

    assert first_sha == second_sha, "idempotent init must not create a new commit"


async def test_init_sets_repo_local_git_identity() -> None:
    """Avoids ``fatal: empty ident name`` on hosts without a global git
    config (CI runners are a common offender). Identity must live at the
    REPO level — that way ``git config --global`` is irrelevant."""
    product_id = uuid.uuid4()
    await init_product_workspace(product_id)
    path = product_workspace_path(product_id)

    name = await _git("config", "--local", "user.name", cwd=path)
    email = await _git("config", "--local", "user.email", cwd=path)
    assert name == "BSVibe Agent"
    assert email == "agent@bsvibe.dev"


# ---------------------------------------------------------------------------
# add_run_worktree
# ---------------------------------------------------------------------------


async def test_add_run_worktree_creates_branch_at_main() -> None:
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)

    worktree = await add_run_worktree(product_id, run_id)
    assert worktree == run_worktree_path(run_id)
    assert worktree.exists()

    # Worktree is on the run branch.
    branch = await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree)
    assert branch == run_branch_name(run_id)
    assert branch.startswith("bsvibe/run/")

    # Worktree starts at main's HEAD.
    product_main_sha = await _git("rev-parse", "main", cwd=product_workspace_path(product_id))
    worktree_sha = await _git("rev-parse", "HEAD", cwd=worktree)
    assert worktree_sha == product_main_sha

    # Initial marker file is checked out.
    assert (worktree / ".bsvibe" / "PRODUCT.md").is_file()


async def test_add_run_worktree_is_idempotent_when_already_registered() -> None:
    """Worker idempotency: an already-existing worktree (e.g. AgentWorker
    re-engaging a run after a crash) returns the same path without
    re-creating anything."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)

    first = await add_run_worktree(product_id, run_id)
    second = await add_run_worktree(product_id, run_id)
    assert first == second


async def test_add_run_worktree_rejects_unregistered_stale_dir(tmp_path) -> None:
    """If the run dir exists but isn't a git worktree (e.g. legacy data
    from pre-W1 runs), refuse — we don't auto-delete user data."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)

    stale = run_worktree_path(run_id)
    stale.mkdir(parents=True)
    (stale / "legacy.txt").write_text("from before W1")

    with pytest.raises(ProductWorkspaceError):
        await add_run_worktree(product_id, run_id)


async def test_add_run_worktree_requires_initialised_product() -> None:
    """A worktree off a non-existent product workspace must fail loudly
    — silent recovery would obscure a wiring bug."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    # No init_product_workspace call.

    with pytest.raises(ProductWorkspaceError):
        await add_run_worktree(product_id, run_id)


# ---------------------------------------------------------------------------
# remove_run_worktree
# ---------------------------------------------------------------------------


async def test_remove_run_worktree_cleans_dir_and_branch() -> None:
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    await add_run_worktree(product_id, run_id)
    assert run_worktree_path(run_id).exists()

    await remove_run_worktree(product_id, run_id)

    assert not run_worktree_path(run_id).exists()
    # Branch is gone.
    proc = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "--verify",
        run_branch_name(run_id),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(product_workspace_path(product_id)),
    )
    await proc.communicate()
    assert proc.returncode != 0, "branch should be deleted"


async def test_remove_run_worktree_is_idempotent_when_missing() -> None:
    """A second remove on the same run is a no-op (covers crash-then-retry
    in the worker's cleanup tick)."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    await add_run_worktree(product_id, run_id)

    await remove_run_worktree(product_id, run_id)
    await remove_run_worktree(product_id, run_id)  # must not raise


async def test_remove_run_worktree_keeps_branch_when_delete_branch_false() -> None:
    """``delete_branch=False`` lets the caller hand a branch off to e.g.
    a github push step that needs the branch alive."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    await add_run_worktree(product_id, run_id)

    # Make a commit in the worktree so the branch has its own HEAD.
    worktree = run_worktree_path(run_id)
    (worktree / "test.txt").write_text("agent's work")
    await _git("add", "-A", cwd=worktree)
    await _git("commit", "-m", "agent commit", cwd=worktree)

    await remove_run_worktree(product_id, run_id, delete_branch=False)

    # Worktree dir is gone but branch survives.
    assert not run_worktree_path(run_id).exists()
    branch_sha = await _git(
        "rev-parse",
        run_branch_name(run_id),
        cwd=product_workspace_path(product_id),
    )
    assert branch_sha, "branch must still exist"


# ---------------------------------------------------------------------------
# End-to-end: agent writes in worktree, product main is untouched
# ---------------------------------------------------------------------------


async def test_worktree_writes_do_not_touch_main_until_merge() -> None:
    """Branching invariant: agent's writes in the run worktree do NOT
    appear on the product's main branch until a merge happens. (Merge
    itself is W2 — this test just locks in the W1 isolation property.)"""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)

    # Agent writes + commits a file in the worktree.
    (worktree / "agent-file.txt").write_text("hello from the agent")
    await _git("add", "-A", cwd=worktree)
    await _git("commit", "-m", "agent: add agent-file.txt", cwd=worktree)

    # Product main DOES NOT have the file. Two separate checks:
    # 1. The file isn't physically present at the product main checkout.
    product_path = product_workspace_path(product_id)
    assert not (product_path / "agent-file.txt").exists()
    # 2. The main branch's tree doesn't list it.
    main_tree = await _git("ls-tree", "-r", "--name-only", "main", cwd=product_path)
    assert "agent-file.txt" not in main_tree.splitlines()


# ---------------------------------------------------------------------------
# list_product_tree — lazy per-directory listing of product main
# ---------------------------------------------------------------------------


async def _commit_to_main(product_id, files: dict[str, str]) -> None:
    """Write + commit ``{path: content}`` onto the product's main checkout."""
    repo = product_workspace_path(product_id)
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", "seed files", cwd=repo)


async def test_list_product_tree_lists_one_level_dirs_before_files() -> None:
    product_id = uuid.uuid4()
    await init_product_workspace(product_id)
    await _commit_to_main(
        product_id,
        {"README.md": "# hi\n", "src/app.py": "x = 1\n", "src/util/io.py": "y = 2\n"},
    )

    root = await list_product_tree(product_id)
    # Directories sort before files; .bsvibe + src are dirs, README.md a file.
    assert [(e.name, e.kind) for e in root] == [
        (".bsvibe", "dir"),
        ("src", "dir"),
        ("README.md", "file"),
    ]
    # One level only: src/util is NOT walked into at the root.
    assert all(e.path in (".bsvibe", "src", "README.md") for e in root)

    # Listing a subdir returns its immediate children (full repo-relative path).
    src = await list_product_tree(product_id, "src")
    assert [(e.name, e.path, e.kind) for e in src] == [
        ("util", "src/util", "dir"),
        ("app.py", "src/app.py", "file"),
    ]


async def test_list_product_tree_uninitialised_product_returns_empty() -> None:
    # No init_product_workspace → no repo → calm empty list, never raises.
    assert await list_product_tree(uuid.uuid4()) == []


async def test_list_product_tree_rejects_traversal() -> None:
    product_id = uuid.uuid4()
    await init_product_workspace(product_id)
    assert await list_product_tree(product_id, "../..") == []
    assert await list_product_tree(product_id, "/etc") == []
