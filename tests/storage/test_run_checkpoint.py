"""#929 (B), stage 1 — a run's worktree survives moving to another machine.

The worktree is the one piece of a run that lives only on the machine driving it: before the
first verify every edit is uncommitted, and the run branch reaches R2 only when the run ships.
A second machine that took the run would branch fresh from ``main`` and silently lose the work.

``checkpoint_run_worktree`` records the branch AND the uncommitted working tree in a run-scoped
bundle — without touching the branch: the snapshot is a separate commit object under
``refs/bsvibe/checkpoints/<rid>``, so no "WIP" commit ever reaches ``main`` when the run ships.
``restore_run_worktree`` rebuilds the worktree on another machine: the branch as it was, and the
uncommitted changes as uncommitted changes (deletions included). Build byproducts (``.venv``,
caches) are never carried. See docs/design/stateless-workers.md §2.2–2.3.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.config import get_settings
from backend.storage.product_bundle_store import (
    LocalFilesystemBundleStore,
    S3BundleStore,
    build_run_checkpoint_store,
)
from backend.storage.product_workspace import (
    add_run_worktree,
    checkpoint_run_worktree,
    delete_run_checkpoint,
    ensure_product_workspace,
    init_product_workspace,
    product_workspace_path,
    publish_product_bundle,
    restore_run_worktree,
    run_worktree_path,
)

pytestmark = pytest.mark.asyncio


async def _git(*args: str, cwd: Path) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    # rstrip only: porcelain status lines start with a meaningful space.
    return out.decode().rstrip()


class _Machine:
    def __init__(self, base: Path, mp: pytest.MonkeyPatch) -> None:
        self.base, self._mp = base, mp

    def __enter__(self) -> _Machine:
        s = get_settings()
        self._mp.setattr(s, "product_workspace_root", str(self.base / "products"), raising=False)
        self._mp.setattr(s, "run_workspace_root", str(self.base / "runs"), raising=False)
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return {
        "products": LocalFilesystemBundleStore(tmp_path / "bundles"),
        "runs": LocalFilesystemBundleStore(tmp_path / "bundles" / "runs"),
        "a": _Machine(tmp_path / "a", monkeypatch),
        "b": _Machine(tmp_path / "b", monkeypatch),
    }


async def _run_with_work(world: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID, str]:
    """On machine A: a product with tracked files, a run with one real commit, then
    uncommitted edits — modified, new, deleted — plus build byproducts."""
    pid, rid = uuid.uuid4(), uuid.uuid4()
    with world["a"]:
        await init_product_workspace(pid)
        repo = product_workspace_path(pid)
        for name in ("keep.py", "edit.py", "gone.py"):
            (repo / name).write_text(f"{name} v1\n")
        await _git("add", ".", cwd=repo)
        await _git("commit", "-qm", "base files", cwd=repo)
        await publish_product_bundle(pid, store=world["products"])
        wt = await add_run_worktree(pid, rid)
        (wt / "committed.py").write_text("verified work\n")
        await _git("add", "committed.py", cwd=wt)
        await _git("commit", "-qm", "real run commit", cwd=wt)
        head = await _git("rev-parse", "HEAD", cwd=wt)
        (wt / "edit.py").write_text("edit.py v2 (uncommitted)\n")
        (wt / "new.py").write_text("brand new (untracked)\n")
        (wt / "gone.py").unlink()
        (wt / ".venv").mkdir()
        (wt / ".venv" / "big.bin").write_text("x" * 100)
        (wt / "__pycache__").mkdir()
        (wt / "__pycache__" / "m.pyc").write_text("pyc")
    return pid, rid, head


async def test_a_checkpoint_leaves_the_run_branch_and_tree_untouched(world: dict[str, Any]) -> None:
    pid, rid, head = await _run_with_work(world)
    with world["a"]:
        wt = run_worktree_path(rid)
        before = await _git("status", "--porcelain", cwd=wt)

        sha = await checkpoint_run_worktree(pid, rid, store=world["runs"])

        assert sha is not None
        assert await _git("rev-parse", "HEAD", cwd=wt) == head, "no WIP commit on the branch"
        assert await _git("status", "--porcelain", cwd=wt) == before
        assert await _git("log", "--format=%s", "main..HEAD", cwd=wt) == "real run commit"


async def test_another_machine_restores_the_branch_and_the_uncommitted_work(
    world: dict[str, Any],
) -> None:
    pid, rid, head = await _run_with_work(world)
    with world["a"]:
        await checkpoint_run_worktree(pid, rid, store=world["runs"])

    with world["b"]:
        assert await ensure_product_workspace(pid, store=world["products"])
        assert await restore_run_worktree(pid, rid, store=world["runs"]) is True
        wt = run_worktree_path(rid)
        assert await _git("rev-parse", "HEAD", cwd=wt) == head, "the branch as it was"
        assert await _git("log", "--format=%s", "main..HEAD", cwd=wt) == "real run commit"
        assert (wt / "committed.py").read_text() == "verified work\n"
        assert (wt / "edit.py").read_text() == "edit.py v2 (uncommitted)\n"
        assert (wt / "new.py").read_text() == "brand new (untracked)\n"
        assert not (wt / "gone.py").exists(), "a deletion is part of the work"
        assert (wt / "keep.py").read_text() == "keep.py v1\n"
        assert not (wt / ".venv").exists() and not (wt / "__pycache__").exists()
        # ...and they are still UNCOMMITTED work, not a commit someone has to undo.
        status = await _git("status", "--porcelain", cwd=wt)
        assert " M edit.py" in status and "?? new.py" in status and " D gone.py" in status


async def test_no_checkpoint_means_nothing_to_restore(world: dict[str, Any]) -> None:
    pid, rid, _ = await _run_with_work(world)
    with world["b"]:
        await ensure_product_workspace(pid, store=world["products"])
        assert await restore_run_worktree(pid, rid, store=world["runs"]) is False
        assert not run_worktree_path(rid).exists()


async def test_a_missing_worktree_has_nothing_to_checkpoint(world: dict[str, Any]) -> None:
    with world["a"]:
        assert (
            await checkpoint_run_worktree(uuid.uuid4(), uuid.uuid4(), store=world["runs"]) is None
        )


async def test_the_latest_checkpoint_wins(world: dict[str, Any]) -> None:
    pid, rid, _ = await _run_with_work(world)
    with world["a"]:
        wt = run_worktree_path(rid)
        await checkpoint_run_worktree(pid, rid, store=world["runs"])
        (wt / "edit.py").write_text("edit.py v3\n")
        await checkpoint_run_worktree(pid, rid, store=world["runs"])
    with world["b"]:
        await ensure_product_workspace(pid, store=world["products"])
        await restore_run_worktree(pid, rid, store=world["runs"])
        assert (run_worktree_path(rid) / "edit.py").read_text() == "edit.py v3\n"


async def test_a_checkpoint_can_be_deleted(world: dict[str, Any]) -> None:
    pid, rid, _ = await _run_with_work(world)
    with world["a"]:
        await checkpoint_run_worktree(pid, rid, store=world["runs"])
    await delete_run_checkpoint(rid, store=world["runs"])
    assert await world["runs"].exists(rid) is False


def test_run_checkpoints_live_under_their_own_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "product_bundle_backend", "s3", raising=False)
    for key, value in (
        ("product_bundle_s3_endpoint", "https://acct.r2.example.com"),
        ("product_bundle_s3_bucket", "b"),
        ("product_bundle_s3_access_key", "AK"),
        ("product_bundle_s3_secret_key", "SK"),
    ):
        monkeypatch.setattr(s, key, value, raising=False)
    store = build_run_checkpoint_store(s)
    rid = uuid.UUID("2cad16bd-1258-4ab9-8f7d-74d403847354")

    assert isinstance(store, S3BundleStore)
    assert store._object_key(rid) == f"runs/{rid}.bundle"


async def test_a_run_released_on_one_machine_continues_on_another(
    world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole policy with nothing faked: A's release saves, B's provision restores."""
    from types import SimpleNamespace

    from backend.workflow.application import run_checkpoint

    s = get_settings()
    monkeypatch.setattr(s, "product_bundle_backend", "local", raising=False)
    monkeypatch.setattr(s, "product_bundle_local_root", str(tmp_path / "bundles"), raising=False)

    async def _not_client_attach(session: Any, pid: uuid.UUID) -> bool:
        return False

    monkeypatch.setattr(run_checkpoint, "product_is_client_attach", _not_client_attach)
    pid, rid, head = await _run_with_work(world)
    run = SimpleNamespace(id=rid, product_id=pid, payload={})

    with world["a"]:
        await run_checkpoint.checkpoint_before_release(object(), run, machine_id="box-a")
    assert run.payload["checkpoint"]["machine"] == "box-a"

    with world["b"]:
        await ensure_product_workspace(pid, store=world["products"])
        wt = run_worktree_path(rid)
        await run_checkpoint.provision_run_worktree(run, workspace_dir=wt, machine_id="box-b")
        assert await _git("rev-parse", "HEAD", cwd=wt) == head
        assert (wt / "edit.py").read_text() == "edit.py v2 (uncommitted)\n"


async def test_removing_a_finished_runs_tree_removes_its_checkpoint(
    world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.storage.product_workspace import remove_run_worktree

    s = get_settings()
    monkeypatch.setattr(s, "product_bundle_backend", "local", raising=False)
    monkeypatch.setattr(s, "product_bundle_local_root", str(tmp_path / "bundles"), raising=False)
    monkeypatch.setattr(s, "machine_id", "box-a", raising=False)
    pid, rid, _ = await _run_with_work(world)
    with world["a"]:
        await checkpoint_run_worktree(pid, rid, store=world["runs"])
        assert await world["runs"].exists(rid)

        await remove_run_worktree(pid, rid)

    assert await world["runs"].exists(rid) is False


# --- reading one file from a checkpoint (no worktree) -------------------------------------------


async def test_a_file_reads_straight_from_the_checkpoint(world: dict[str, Any]) -> None:
    """Another machine serving the run's artifact (the Files viewer, MCP) reads it from the
    checkpoint — committed or not — without restoring a worktree."""
    from backend.storage.product_workspace import read_run_checkpoint_file

    pid, rid, _ = await _run_with_work(world)
    with world["a"]:
        await checkpoint_run_worktree(pid, rid, store=world["runs"])

    with world["b"]:
        assert await read_run_checkpoint_file(rid, "edit.py", store=world["runs"]) == (
            b"edit.py v2 (uncommitted)\n"
        )
        assert await read_run_checkpoint_file(rid, "committed.py", store=world["runs"]) == (
            b"verified work\n"
        )
        assert await read_run_checkpoint_file(rid, "gone.py", store=world["runs"]) is None
        assert await read_run_checkpoint_file(rid, ".venv/big.bin", store=world["runs"]) is None
        assert not run_worktree_path(rid).exists(), "no worktree was made to read it"


async def test_no_checkpoint_reads_nothing(world: dict[str, Any]) -> None:
    from backend.storage.product_workspace import read_run_checkpoint_file

    with world["b"]:
        assert await read_run_checkpoint_file(uuid.uuid4(), "a.py", store=world["runs"]) is None


@pytest.mark.parametrize("bad", ["../escape.py", "/etc/passwd", "a/../../b.py"])
async def test_a_checkpoint_read_cannot_leave_the_tree(world: dict[str, Any], bad: str) -> None:
    from backend.storage.product_workspace import read_run_checkpoint_file

    with pytest.raises(ValueError):
        await read_run_checkpoint_file(uuid.uuid4(), bad, store=world["runs"])
