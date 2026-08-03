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
import shutil
import uuid

import pytest

from backend.config import get_settings
from backend.storage.product_workspace import (
    ProductWorkspaceError,
    add_run_worktree,
    init_product_workspace,
    list_product_tree,
    product_workspace_path,
    remove_product_workspace,
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


async def test_remove_run_worktree_reclaims_github_clone_dir() -> None:
    """A github run's workspace is a FULL CLONE (its own ``.git`` dir), NOT a
    linked worktree of a product repo — ``git worktree remove`` cannot recognise
    it, so the historical leak was 137 such clone dirs. The removal must fall
    back to a plain ``rmtree`` and reclaim the dir even when there is no product
    workspace at all (github products keep no local ``var/products/<id>`` repo)."""
    product_id = uuid.uuid4()  # never init_product_workspace'd → no product repo
    run_id = uuid.uuid4()

    # Simulate the per-run github clone landing directly in var/runs/<id>.
    clone = run_worktree_path(run_id)
    clone.mkdir(parents=True)
    (clone / ".git").mkdir()  # a real clone → its own .git DIRECTORY
    (clone / "README.md").write_text("cloned repo")
    assert clone.exists()

    await remove_run_worktree(product_id, run_id)  # must not raise

    assert not clone.exists(), "github clone dir must be reclaimed via rmtree"


async def test_remove_run_worktree_reclaims_dir_with_no_product_id() -> None:
    """The reaper may reap a run whose ``product_id`` is NULL; removal must
    still reclaim the on-disk dir without a product repo to consult."""
    run_id = uuid.uuid4()
    clone = run_worktree_path(run_id)
    clone.mkdir(parents=True)
    (clone / "work.txt").write_text("scratch")

    await remove_run_worktree(None, run_id)  # must not raise

    assert not clone.exists()


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


# ---------------------------------------------------------------------------
# remove_product_workspace — the product repo is reclaimable
# ---------------------------------------------------------------------------


async def test_remove_product_workspace_reclaims_repo() -> None:
    """Deleting a product must reclaim its on-disk repo. Production kept every
    deleted product's repo forever — 18 such dirs (300MB) were found live."""
    product_id = uuid.uuid4()
    await init_product_workspace(product_id)
    path = product_workspace_path(product_id)
    assert path.is_dir()

    await remove_product_workspace(product_id)

    assert not path.exists()


async def test_remove_product_workspace_is_idempotent() -> None:
    """A second remove (crash-then-retry, or a product that never had a repo)
    is a no-op, not an error."""
    product_id = uuid.uuid4()
    await remove_product_workspace(product_id)  # never existed
    await init_product_workspace(product_id)
    await remove_product_workspace(product_id)
    await remove_product_workspace(product_id)  # must not raise


# ---------------------------------------------------------------------------
# push_product_bundle — the product's state, made durable off-box
# ---------------------------------------------------------------------------


async def test_push_product_bundle_round_trips_the_whole_repo(tmp_path) -> None:
    """The pushed bundle must restore a COMPLETE repo — same HEAD, same
    history, branches intact. That property is what keeps git's merge/conflict
    machinery alive across a materialise → work → persist cycle; a working-tree
    snapshot would force last-write-wins and lose concurrent work."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import push_product_bundle

    product_id = uuid.uuid4()
    await init_product_workspace(product_id)
    repo = product_workspace_path(product_id)
    (repo / "app.py").write_text("print('v1')\n")
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", "feat: app", cwd=repo)
    head = await _git("rev-parse", "HEAD", cwd=repo)

    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    await push_product_bundle(product_id, store=store)

    assert await store.exists(product_id) is True
    fetched = tmp_path / "fetched.bundle"
    assert await store.get(product_id, fetched) is True

    restored = tmp_path / "restored"
    await _git("clone", str(fetched), str(restored), cwd=tmp_path)
    assert await _git("rev-parse", "HEAD", cwd=restored) == head
    assert (restored / "app.py").read_text() == "print('v1')\n"


async def test_push_product_bundle_includes_run_branches(tmp_path) -> None:
    """``--all``, not just ``main``: an in-flight run's branch must survive the
    round-trip or resuming that run after a materialise would lose its work."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import push_product_bundle

    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)
    (worktree / "wip.txt").write_text("in progress\n")
    await _git("add", "-A", cwd=worktree)
    await _git("commit", "-m", "wip", cwd=worktree)

    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    await push_product_bundle(product_id, store=store)

    fetched = tmp_path / "f.bundle"
    await store.get(product_id, fetched)
    restored = tmp_path / "restored"
    await _git("clone", str(fetched), str(restored), cwd=tmp_path)
    branches = await _git("branch", "-a", cwd=restored)
    assert run_branch_name(run_id) in branches


async def test_push_product_bundle_missing_repo_is_noop(tmp_path) -> None:
    """A product whose repo is absent (never provisioned / already reclaimed)
    must not raise — the caller is a ship path that already succeeded."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import push_product_bundle

    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    product_id = uuid.uuid4()
    await push_product_bundle(product_id, store=store)  # must not raise
    assert await store.exists(product_id) is False


# ---------------------------------------------------------------------------
# ensure_product_workspace — restore the repo from its durable home on demand
# ---------------------------------------------------------------------------


async def test_ensure_materialises_repo_from_bundle_when_absent(tmp_path) -> None:
    """The disk holds only what is being worked on; the bundle is the record.
    When the repo is absent, it is restored from the bundle — with its history
    and branches — so everything downstream (merge, conflict detection, file
    browsing) behaves exactly as if it had never left."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import (
        ensure_product_workspace,
        push_product_bundle,
    )

    product_id = uuid.uuid4()
    await init_product_workspace(product_id)
    repo = product_workspace_path(product_id)
    (repo / "app.py").write_text("print('shipped')\n")
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", "feat: app", cwd=repo)
    head = await _git("rev-parse", "HEAD", cwd=repo)

    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    await push_product_bundle(product_id, store=store)

    # The repo leaves the disk entirely (what PR 7/8 will do after every run).
    shutil.rmtree(repo)
    assert not repo.exists()

    assert await ensure_product_workspace(product_id, store=store) is True

    assert (repo / ".git").exists()
    assert (repo / "app.py").read_text() == "print('shipped')\n"
    assert await _git("rev-parse", "HEAD", cwd=repo) == head


async def test_ensure_is_a_noop_when_repo_is_present(tmp_path) -> None:
    """A present repo is the working copy of record — never clobber it with an
    older bundle, or a run's in-progress commits would vanish."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import (
        ensure_product_workspace,
        push_product_bundle,
    )

    product_id = uuid.uuid4()
    await init_product_workspace(product_id)
    repo = product_workspace_path(product_id)
    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    await push_product_bundle(product_id, store=store)  # bundle == empty product

    # Local work lands AFTER the bundle was published.
    (repo / "newer.py").write_text("local work\n")
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", "newer", cwd=repo)

    assert await ensure_product_workspace(product_id, store=store) is True

    assert (repo / "newer.py").exists(), "a present repo must not be overwritten"


async def test_ensure_returns_false_when_no_bundle_exists(tmp_path) -> None:
    """A product that never shipped has no bundle. That is not an error — the
    caller decides (provision an empty repo, or surface an empty file tree)."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import ensure_product_workspace

    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    assert await ensure_product_workspace(uuid.uuid4(), store=store) is False


async def test_ensure_preserves_run_branches_through_the_round_trip(tmp_path) -> None:
    """An in-flight run's branch must survive repo → bundle → repo, or resuming
    that run after a materialise would silently lose its commits."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import (
        ensure_product_workspace,
        push_product_bundle,
    )

    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)
    (worktree / "wip.txt").write_text("in progress\n")
    await _git("add", "-A", cwd=worktree)
    await _git("commit", "-m", "wip", cwd=worktree)

    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    await push_product_bundle(product_id, store=store)

    shutil.rmtree(product_workspace_path(product_id))
    shutil.rmtree(worktree, ignore_errors=True)
    assert await ensure_product_workspace(product_id, store=store) is True

    branches = await _git("branch", "-a", cwd=product_workspace_path(product_id))
    assert run_branch_name(run_id) in branches


async def test_list_product_tree_materialises_instead_of_failing_open(tmp_path) -> None:
    """``list_product_tree`` used to return ``[]`` for a missing repo, so the
    PWA showed an EMPTY product with no error. Once the repo lives off-box that
    silent lie would be the normal case — restore it instead."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import push_product_bundle

    product_id = uuid.uuid4()
    await init_product_workspace(product_id)
    repo = product_workspace_path(product_id)
    (repo / "README.md").write_text("# hi\n")
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", "docs", cwd=repo)

    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    await push_product_bundle(product_id, store=store)
    shutil.rmtree(repo)

    entries = await list_product_tree(product_id, store=store)

    names = {e.name for e in entries}
    assert "README.md" in names, f"expected the restored tree, got {names}"


# ---------------------------------------------------------------------------
# publish_product_bundle — merge onto the latest, never blob-overwrite
# ---------------------------------------------------------------------------


async def _bundle_of(repo, dest) -> None:
    await _git("bundle", "create", str(dest), "--all", cwd=repo)


async def test_publish_merges_concurrent_work_instead_of_losing_it(tmp_path) -> None:
    """THE data-loss case a plain overwrite cannot survive.

    Someone else published work (rev B) after this box materialised its copy
    (rev A). A blob-level ``put`` would overwrite B with A+local and B's commit
    would be gone forever. Publishing must MERGE onto whatever is currently in
    the store, so both survive."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import publish_product_bundle

    product_id = uuid.uuid4()
    store = LocalFilesystemBundleStore(tmp_path / "bundles")

    # rev A — the common ancestor, published.
    await init_product_workspace(product_id)
    repo = product_workspace_path(product_id)
    (repo / "base.txt").write_text("base\n")
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", "base", cwd=repo)
    a_bundle = tmp_path / "a.bundle"
    await _bundle_of(repo, a_bundle)
    await store.put(product_id, a_bundle)

    # Someone else clones rev A elsewhere, adds THEIR file, publishes rev B.
    other = tmp_path / "other"
    await _git("clone", str(a_bundle), str(other), cwd=tmp_path)
    await _git("config", "user.email", "o@e.dev", cwd=other)
    await _git("config", "user.name", "Other", cwd=other)
    (other / "theirs.txt").write_text("their work\n")
    await _git("add", "-A", cwd=other)
    await _git("commit", "-m", "theirs", cwd=other)
    b_bundle = tmp_path / "b.bundle"
    await _bundle_of(other, b_bundle)
    await store.put(product_id, b_bundle)

    # Meanwhile THIS box (still on rev A) does its own work and publishes.
    (repo / "ours.txt").write_text("our work\n")
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", "ours", cwd=repo)

    outcome = await publish_product_bundle(product_id, store=store)
    assert outcome.status == "clean"

    # Both survive in the published bundle.
    final = tmp_path / "final.bundle"
    assert await store.get(product_id, final) is True
    check = tmp_path / "check"
    await _git("clone", str(final), str(check), cwd=tmp_path)
    assert (check / "ours.txt").exists(), "our work must be published"
    assert (check / "theirs.txt").exists(), "concurrent work must NOT be lost"


async def test_publish_reports_conflict_and_leaves_the_store_untouched(tmp_path) -> None:
    """When the two sides edited the same lines, the merge cannot be decided
    here. Report the conflict and publish NOTHING — a half-merged or
    arbitrarily-resolved bundle would be worse than a stale one, and the local
    repo stays authoritative until a human decides."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import publish_product_bundle

    product_id = uuid.uuid4()
    store = LocalFilesystemBundleStore(tmp_path / "bundles")

    await init_product_workspace(product_id)
    repo = product_workspace_path(product_id)
    (repo / "shared.txt").write_text("original\n")
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", "base", cwd=repo)
    a_bundle = tmp_path / "a.bundle"
    await _bundle_of(repo, a_bundle)
    await store.put(product_id, a_bundle)

    other = tmp_path / "other"
    await _git("clone", str(a_bundle), str(other), cwd=tmp_path)
    await _git("config", "user.email", "o@e.dev", cwd=other)
    await _git("config", "user.name", "Other", cwd=other)
    (other / "shared.txt").write_text("THEIR version\n")
    await _git("add", "-A", cwd=other)
    await _git("commit", "-m", "theirs", cwd=other)
    b_bundle = tmp_path / "b.bundle"
    await _bundle_of(other, b_bundle)
    await store.put(product_id, b_bundle)
    b_bytes = b_bundle.read_bytes()

    (repo / "shared.txt").write_text("OUR version\n")
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", "ours", cwd=repo)

    outcome = await publish_product_bundle(product_id, store=store)

    assert outcome.status == "conflict"
    assert "shared.txt" in outcome.conflict_paths
    # The store still holds rev B, untouched.
    kept = tmp_path / "kept.bundle"
    await store.get(product_id, kept)
    assert kept.read_bytes() == b_bytes, "a conflicted publish must not overwrite"
    # And the local repo is left clean (no half-merge stranded on disk).
    status = await _git("status", "--porcelain", cwd=repo)
    assert "UU" not in status, f"merge must be aborted, got: {status}"


async def test_publish_first_time_just_uploads(tmp_path) -> None:
    """No bundle in the store yet (the product's first ship) — nothing to merge
    onto, so publish straight."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import publish_product_bundle

    product_id = uuid.uuid4()
    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    await init_product_workspace(product_id)
    repo = product_workspace_path(product_id)
    (repo / "first.txt").write_text("first\n")
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", "first", cwd=repo)

    outcome = await publish_product_bundle(product_id, store=store)

    assert outcome.status == "clean"
    assert await store.exists(product_id) is True


async def test_publish_no_repo_is_a_noop(tmp_path) -> None:
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import publish_product_bundle

    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    outcome = await publish_product_bundle(uuid.uuid4(), store=store)
    assert outcome.status == "clean"
    assert outcome.published is False


# ---------------------------------------------------------------------------
# Shallow repos must never be published — their bundle cannot be restored
# ---------------------------------------------------------------------------


async def test_publish_refuses_a_shallow_repo(tmp_path) -> None:
    """A repo cloned with ``--depth=1`` (what product bootstrap does for a
    repo_url) bundles WITHOUT the parents its own commits reference. The bundle
    is created happily, ``git bundle verify`` even calls it "a complete
    history", and the failure only appears on restore:

        fatal: remote did not send all necessary objects

    Found in production: a product was published, its repo reclaimed on the
    strength of that publish, and the bundle turned out to be unrestorable.
    Refusing to publish keeps the reclaim gate shut, so the repo stays."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import publish_product_bundle

    # Build an upstream with history, then a SHALLOW clone of it (the bootstrap
    # shape). ``product_workspace_path`` is where the shallow clone must land.
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    await _git("init", "--initial-branch=main", cwd=upstream)
    await _git("config", "user.email", "u@e.dev", cwd=upstream)
    await _git("config", "user.name", "U", cwd=upstream)
    for i in range(3):
        (upstream / f"f{i}.txt").write_text(f"{i}\n")
        await _git("add", "-A", cwd=upstream)
        await _git("commit", "-m", f"c{i}", cwd=upstream)

    product_id = uuid.uuid4()
    repo = product_workspace_path(product_id)
    repo.parent.mkdir(parents=True, exist_ok=True)
    await _git("clone", "--depth", "1", f"file://{upstream}", str(repo), cwd=tmp_path)
    assert (await _git("rev-parse", "--is-shallow-repository", cwd=repo)) == "true", (
        "fixture must actually be shallow"
    )
    # ...and UNREPAIRABLE: the upstream is unreachable, so ``fetch --unshallow``
    # cannot fill in the missing history. (When the upstream IS reachable the
    # publish repairs the repo and proceeds — the next test covers that.)
    await _git("remote", "remove", "origin", cwd=repo)

    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    outcome = await publish_product_bundle(product_id, store=store)

    assert outcome.published is False, "a shallow repo must not be published"
    assert await store.exists(product_id) is False, (
        "no unrestorable bundle may be left in the store"
    )


async def test_publish_accepts_a_repo_unshallowed_first(tmp_path) -> None:
    """The same shallow repo, when its upstream IS reachable, is REPAIRED by the
    publish (fetch --unshallow) and then publishes normally — the guard is about
    restorability, not about where the repo came from."""
    from backend.storage.product_bundle_store import LocalFilesystemBundleStore
    from backend.storage.product_workspace import publish_product_bundle

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    await _git("init", "--initial-branch=main", cwd=upstream)
    await _git("config", "user.email", "u@e.dev", cwd=upstream)
    await _git("config", "user.name", "U", cwd=upstream)
    for i in range(3):
        (upstream / f"f{i}.txt").write_text(f"{i}\n")
        await _git("add", "-A", cwd=upstream)
        await _git("commit", "-m", f"c{i}", cwd=upstream)

    product_id = uuid.uuid4()
    repo = product_workspace_path(product_id)
    repo.parent.mkdir(parents=True, exist_ok=True)
    await _git("clone", "--depth", "1", f"file://{upstream}", str(repo), cwd=tmp_path)
    # NOTE: no manual unshallow — the publish is expected to repair it.

    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    outcome = await publish_product_bundle(product_id, store=store)

    assert outcome.published is True
    # And the bundle genuinely restores.
    fetched = tmp_path / "f.bundle"
    assert await store.get(product_id, fetched) is True
    restored = tmp_path / "restored"
    await _git("clone", str(fetched), str(restored), cwd=tmp_path)
    assert (restored / "f2.txt").exists()
