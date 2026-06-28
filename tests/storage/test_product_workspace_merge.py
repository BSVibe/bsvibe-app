"""W2 — verify-time merge + ship merge tests.

Exercises the new merge helpers in
:mod:`backend.storage.product_workspace`:

* ``commit_worktree`` stages agent writes as branch commits
* ``merge_main_into_worktree`` detects pre-ship conflicts
* ``merge_to_main`` fast-forwards main on clean ships
* ``force_merge_theirs`` overrides main with run's version
* ``product_workspace_lock`` serializes parallel ship attempts

All against ``tmp_path`` — no PG / no sandbox needed. Each test sets up
a product workspace + one or two worktrees, simulates agent writes via
direct file I/O, then drives the merge functions and asserts on git's
own state.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.config import get_settings
from backend.storage.product_workspace import (
    ProductWorkspaceBusy,
    abort_merge,
    add_run_worktree,
    capture_run_diff,
    commit_worktree,
    force_merge_theirs,
    init_product_workspace,
    merge_main_into_worktree,
    merge_to_main,
    product_workspace_lock,
    product_workspace_path,
    run_branch_name,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolate_workspace_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(
        get_settings(), "product_workspace_root", str(tmp_path / "products"), raising=False
    )
    monkeypatch.setattr(get_settings(), "run_workspace_root", str(tmp_path / "runs"), raising=False)


async def _git(*args: str, cwd) -> str:
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
# commit_worktree
# ---------------------------------------------------------------------------


async def test_commit_worktree_stages_and_commits_agent_writes() -> None:
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)

    (worktree / "hello.py").write_text("def add(a, b):\n    return a + b\n")
    sha = await commit_worktree(product_id, run_id, message="agent: add() function")
    assert sha is not None and len(sha) == 40

    # The commit is on the run branch.
    branch_head = await _git("rev-parse", run_branch_name(run_id), cwd=worktree)
    assert branch_head == sha
    # Main is unchanged — pre-merge isolation invariant.
    product = product_workspace_path(product_id)
    assert await _git("rev-parse", "main", cwd=product) != sha


async def test_commit_worktree_is_noop_when_nothing_changed() -> None:
    """An agent round that only reads files (no writes) must not create
    an empty commit — the verify-time merge has nothing to merge anyway."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    await add_run_worktree(product_id, run_id)

    sha = await commit_worktree(product_id, run_id, message="agent: no work")
    assert sha is None


async def test_commit_worktree_excludes_verification_byproducts() -> None:
    """Build caches + the verifier's injected acceptance scaffold must NOT land
    in the run's commit (else they leak into the delivered PR)."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)

    # the agent's REAL change
    (worktree / "mathx.py").write_text("def clamp(v):\n    return v\n")
    # verification byproducts that must be excluded
    (worktree / "__pycache__").mkdir(exist_ok=True)
    (worktree / "__pycache__" / "mathx.cpython-311.pyc").write_bytes(b"\x00\x01")
    (worktree / "tests").mkdir(exist_ok=True)
    (worktree / "tests" / "_bsvibe_independent_acceptance.py").write_text(
        "def test_x():\n    pass\n"
    )
    (worktree / "tests" / "__pycache__").mkdir(exist_ok=True)
    (worktree / "tests" / "__pycache__" / "test_x.pyc").write_bytes(b"\x00")

    sha = await commit_worktree(product_id, run_id, message="work: clamp")
    assert sha is not None

    files = await _git("show", "--name-only", "--pretty=format:", sha, cwd=worktree)
    assert "mathx.py" in files
    assert ".pyc" not in files
    assert "__pycache__" not in files
    assert "_bsvibe_independent_acceptance" not in files


async def test_commit_worktree_noop_when_only_byproducts() -> None:
    """A round that produced ONLY byproducts (no real source change) must not
    create a commit — no empty PR for a verification-only artifact set."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)

    (worktree / "__pycache__").mkdir(exist_ok=True)
    (worktree / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\x00")
    (worktree / "tests").mkdir(exist_ok=True)
    (worktree / "tests" / "_bsvibe_independent_acceptance.py").write_text(
        "def test_x():\n    pass\n"
    )

    sha = await commit_worktree(product_id, run_id, message="byproducts only")
    assert sha is None


# ---------------------------------------------------------------------------
# merge_main_into_worktree — clean
# ---------------------------------------------------------------------------


async def test_merge_main_clean_when_main_unchanged() -> None:
    """main hasn't moved since the worktree was created → merge is clean
    (well: --no-ff still creates a merge commit, but no conflicts)."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)
    (worktree / "hello.py").write_text("agent's work\n")
    await commit_worktree(product_id, run_id, message="agent")

    outcome = await merge_main_into_worktree(product_id, run_id)
    assert outcome.status == "clean"
    assert outcome.conflict_paths == []


async def test_merge_main_clean_when_main_moved_to_unrelated_file() -> None:
    """Main moved (e.g. parallel run shipped) but touched a DIFFERENT file
    than the run worktree — git merges cleanly without conflicts."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)
    product = product_workspace_path(product_id)

    # Agent writes hello.py.
    (worktree / "hello.py").write_text("agent's add()\n")
    await commit_worktree(product_id, run_id, message="agent")

    # Meanwhile main commits README.md directly on main.
    (product / "README.md").write_text("# Product README\n")
    await _git("add", "-A", cwd=product)
    await _git("commit", "-m", "main: add README", cwd=product)

    outcome = await merge_main_into_worktree(product_id, run_id)
    assert outcome.status == "clean"
    # The worktree now carries BOTH files.
    assert (worktree / "hello.py").exists()
    assert (worktree / "README.md").exists()


# ---------------------------------------------------------------------------
# merge_main_into_worktree — conflict
# ---------------------------------------------------------------------------


async def test_merge_main_conflict_surfaces_paths_and_leaves_markers() -> None:
    """Main and the run worktree both touched the SAME file with different
    contents → merge surfaces the conflict. The worktree is LEFT in
    mid-merge state (markers visible) so the agent's next round can
    resolve via file_read/file_edit."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)
    product = product_workspace_path(product_id)

    # Both touch hello.py with different lines.
    (worktree / "hello.py").write_text("def add(a, b):\n    return a + b\n")
    await commit_worktree(product_id, run_id, message="agent: add")

    (product / "hello.py").write_text("def multiply(a, b):\n    return a * b\n")
    await _git("add", "-A", cwd=product)
    await _git("commit", "-m", "main: multiply", cwd=product)

    outcome = await merge_main_into_worktree(product_id, run_id)
    assert outcome.status == "conflict"
    assert outcome.conflict_paths == ["hello.py"]

    # Conflict markers are in the worktree file.
    content = (worktree / "hello.py").read_text()
    assert "<<<<<<<" in content
    assert ">>>>>>>" in content

    # The merge is in progress — abort_merge cleans it for the next test.
    await abort_merge(product_id, run_id)


# ---------------------------------------------------------------------------
# merge_to_main — fast-forward after clean merge_main_into_worktree
# ---------------------------------------------------------------------------


async def test_merge_to_main_fast_forwards_after_clean_merge() -> None:
    """The ship flow: agent commits → verify pulls main → main fast-
    forwards onto the run branch. The run's commit (plus any merge
    commit from --no-ff) is now on main."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)

    (worktree / "hello.py").write_text("agent's work\n")
    await commit_worktree(product_id, run_id, message="agent")
    pre_merge_outcome = await merge_main_into_worktree(product_id, run_id)
    assert pre_merge_outcome.status == "clean"

    main_sha = await merge_to_main(product_id, run_id)
    product = product_workspace_path(product_id)
    assert await _git("rev-parse", "main", cwd=product) == main_sha
    # The file made it to main.
    assert (product / "hello.py").exists()
    assert (product / "hello.py").read_text() == "agent's work\n"


async def test_merge_to_main_rejects_when_branch_is_not_descendant() -> None:
    """If the run branch isn't a descendant of main (i.e. the caller
    skipped ``merge_main_into_worktree`` first), ``--ff-only`` refuses
    to merge — surfaces a typed error for the caller to handle."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)
    product = product_workspace_path(product_id)

    # Agent commit.
    (worktree / "hello.py").write_text("agent\n")
    await commit_worktree(product_id, run_id, message="agent")
    # Main moves WITHOUT being pulled into the worktree.
    (product / "README.md").write_text("main moved\n")
    await _git("add", "-A", cwd=product)
    await _git("commit", "-m", "main moved", cwd=product)

    # merge_to_main now fails (--ff-only refuses).
    from backend.storage.product_workspace import ProductWorkspaceError

    with pytest.raises(ProductWorkspaceError):
        await merge_to_main(product_id, run_id)


# ---------------------------------------------------------------------------
# force_merge_theirs — executor ship_anyway
# ---------------------------------------------------------------------------


async def test_force_merge_theirs_overrides_main_on_conflicting_paths() -> None:
    """``force_merge_theirs`` is the executor B2b ``ship_anyway`` path —
    the founder explicitly accepted the run's version. On conflicting
    paths, the run's content wins; non-conflicting paths merge normally."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)
    product = product_workspace_path(product_id)

    # Both touch hello.py differently — same conflict shape as a normal
    # ship would refuse, but ship_anyway accepts.
    (worktree / "hello.py").write_text("RUN version\n")
    await commit_worktree(product_id, run_id, message="agent")
    (product / "hello.py").write_text("MAIN version\n")
    await _git("add", "-A", cwd=product)
    await _git("commit", "-m", "main", cwd=product)

    main_sha = await force_merge_theirs(product_id, run_id)
    assert main_sha
    # Main now has the run's content.
    assert (product / "hello.py").read_text() == "RUN version\n"


# ---------------------------------------------------------------------------
# product_workspace_lock — serialization
# ---------------------------------------------------------------------------


async def test_product_workspace_lock_busy_raises() -> None:
    """A second acquire on the same product (from a different task) sees
    ProductWorkspaceBusy. The caller (typically a worker tick) retries
    on the next pass — the lock is not blocking by design."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    # In-memory SQLite is enough to exercise the asyncio.Lock fallback
    # path — the helper checks dialect to pick the right primitive.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    product_id = uuid.uuid4()

    async with sessionmaker() as s1, sessionmaker() as s2:
        async with product_workspace_lock(s1, product_id):
            # Holder task: try to acquire from a SEPARATE task (not the
            # registry, just an asyncio task), which the helper detects.
            async def _try_other_task() -> None:
                with pytest.raises(ProductWorkspaceBusy):
                    async with product_workspace_lock(s2, product_id):
                        pass

            await asyncio.create_task(_try_other_task())

    await engine.dispose()


async def test_product_workspace_lock_releases_on_exit() -> None:
    """After the context manager exits, a fresh acquire succeeds."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    product_id = uuid.uuid4()

    async with sessionmaker() as s:
        async with product_workspace_lock(s, product_id):
            pass

        # Second acquire from the same session — should succeed
        # because the first exited cleanly.
        async with product_workspace_lock(s, product_id):
            pass

    await engine.dispose()


# ---------------------------------------------------------------------------
# capture_run_diff — Lift 2a: the run's own changes as a unified diff
# ---------------------------------------------------------------------------


async def test_capture_run_diff_returns_additions_for_new_file() -> None:
    """A freshly produced file appears as a unified diff of additions
    (``git diff main...HEAD`` — the run's own changes vs the merge base)."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)
    (worktree / "hello.py").write_text("def add(a, b):\n    return a + b\n")
    await commit_worktree(product_id, run_id, message="agent: add()")
    await merge_main_into_worktree(product_id, run_id)

    diff = await capture_run_diff(product_id, run_id)
    assert diff is not None
    # Standard unified-diff markers for a new file.
    assert "diff --git a/hello.py b/hello.py" in diff
    assert "new file" in diff
    assert "+def add(a, b):" in diff


async def test_capture_run_diff_shows_modification_as_red_green() -> None:
    """Editing a file that already exists on main shows BOTH the removed
    (red) and added (green) lines — true old↔new, not all-additions."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    product = product_workspace_path(product_id)

    # A base file lands on main BEFORE the run branches.
    (product / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    await _git("add", "-A", cwd=product)
    await _git("commit", "-m", "main: seed calc.py", cwd=product)

    worktree = await add_run_worktree(product_id, run_id)
    # The run rewrites the body of the existing function.
    (worktree / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")
    await commit_worktree(product_id, run_id, message="agent: tweak add()")
    await merge_main_into_worktree(product_id, run_id)

    diff = await capture_run_diff(product_id, run_id)
    assert diff is not None
    assert "-    return a + b" in diff
    assert "+    return a + b + 0" in diff


async def test_capture_run_diff_none_when_nothing_changed() -> None:
    """A run that wrote nothing has no diff to capture → ``None``."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    await add_run_worktree(product_id, run_id)

    diff = await capture_run_diff(product_id, run_id)
    assert diff is None


async def test_capture_run_diff_none_when_worktree_absent() -> None:
    """No worktree on disk (cleaned / non-product run) → ``None``, never raises."""
    diff = await capture_run_diff(uuid.uuid4(), uuid.uuid4())
    assert diff is None
