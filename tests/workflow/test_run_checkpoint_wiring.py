"""#929 (B), stage 1 — where a run's worktree is saved and where it comes back.

The primitives live in ``storage/product_workspace.py`` (``test_run_checkpoint.py``). This pins
how they are wired (docs/design/stateless-workers.md §2.2–2.3):

* a run is checkpointed BEFORE its claim is given up (pause, back to the queue, escalation) —
  after would let another machine claim it before the upload landed;
* the outcome is recorded on the run: the snapshot sha, or ``stale`` when the save failed (the
  latest work then exists only on the machine that held it);
* a machine provisioning a run whose worktree it lacks restores the checkpoint; a run that has
  a checkpoint but cannot be restored, or whose checkpoint is stale, is REFUSED — never
  branched fresh from ``main``; a run with no checkpoint is new and branches from ``main``;
* a single-box deployment (``BSVIBE_MACHINE_ID`` empty) does none of this — no extra uploads.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.workflow.application import run_checkpoint
from backend.workflow.application.run_checkpoint import (
    CHECKPOINT_KEY,
    RunCheckpointMissing,
    checkpoint_before_release,
    provision_run_worktree,
)

pytestmark = pytest.mark.asyncio


def _run(**payload: Any) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        status=SimpleNamespace(value="running"),
        payload=dict(payload),
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    seen: dict[str, list[Any]] = {"checkpoint": [], "restore": [], "add": []}

    async def _checkpoint(pid: uuid.UUID, rid: uuid.UUID) -> str | None:
        seen["checkpoint"].append(rid)
        return "abc123"

    async def _restore(pid: uuid.UUID, rid: uuid.UUID) -> bool:
        seen["restore"].append(rid)
        return seen.get("restore_ok", [True])[-1]

    async def _add(pid: uuid.UUID, rid: uuid.UUID) -> Path:
        seen["add"].append(rid)
        return Path("/x")

    async def _not_client_attach(session: Any, pid: uuid.UUID) -> bool:
        return False

    monkeypatch.setattr(run_checkpoint, "checkpoint_run_worktree", _checkpoint)
    monkeypatch.setattr(run_checkpoint, "restore_run_worktree", _restore)
    monkeypatch.setattr(run_checkpoint, "add_run_worktree", _add)
    monkeypatch.setattr(run_checkpoint, "product_is_client_attach", _not_client_attach)
    return seen


# --- checkpoint on release --------------------------------------------------------------------


async def test_a_released_run_is_checkpointed_and_says_where(calls) -> None:
    run = _run()

    await checkpoint_before_release(None, run, machine_id="box-a")

    assert calls["checkpoint"] == [run.id]
    assert run.payload[CHECKPOINT_KEY] == {"sha": "abc123", "machine": "box-a"}


async def test_a_single_box_saves_nothing(calls) -> None:
    run = _run()

    await checkpoint_before_release(None, run, machine_id="")

    assert calls["checkpoint"] == []
    assert CHECKPOINT_KEY not in run.payload


async def test_a_failed_save_marks_the_checkpoint_stale(calls, monkeypatch) -> None:
    async def _boom(pid: uuid.UUID, rid: uuid.UUID) -> str | None:
        raise RuntimeError("R2 down")

    monkeypatch.setattr(run_checkpoint, "checkpoint_run_worktree", _boom)
    run = _run(**{CHECKPOINT_KEY: {"sha": "old", "machine": "box-a"}})

    await checkpoint_before_release(None, run, machine_id="box-a")

    assert run.payload[CHECKPOINT_KEY] == {"sha": "old", "machine": "box-a", "stale": True}


async def test_a_run_without_a_product_has_no_tree_to_save(calls) -> None:
    run = _run()
    run.product_id = None

    await checkpoint_before_release(None, run, machine_id="box-a")

    assert calls["checkpoint"] == []


# --- restore on arrival ------------------------------------------------------------------------


async def test_a_new_run_branches_from_main(calls, tmp_path: Path) -> None:
    run = _run()

    await provision_run_worktree(run, workspace_dir=tmp_path / "missing", machine_id="box-b")

    assert calls["add"] == [run.id]


async def test_a_moved_run_is_restored_not_branched_fresh(calls, tmp_path: Path) -> None:
    run = _run(**{CHECKPOINT_KEY: {"sha": "abc123", "machine": "box-a"}})

    await provision_run_worktree(run, workspace_dir=tmp_path / "missing", machine_id="box-b")

    assert calls["restore"] == [run.id]
    assert calls["add"] == [run.id], "add_run_worktree then just re-registers the restored tree"


async def test_a_checkpoint_that_cannot_be_restored_is_refused(calls, tmp_path: Path) -> None:
    calls["restore_ok"] = [False]
    run = _run(**{CHECKPOINT_KEY: {"sha": "abc123", "machine": "box-a"}})

    with pytest.raises(RunCheckpointMissing):
        await provision_run_worktree(run, workspace_dir=tmp_path / "missing", machine_id="box-b")

    assert calls["add"] == [], "never branch fresh from main for a run that already did work"


async def test_a_stale_checkpoint_elsewhere_is_refused(calls, tmp_path: Path) -> None:
    run = _run(**{CHECKPOINT_KEY: {"sha": "old", "machine": "box-a", "stale": True}})

    with pytest.raises(RunCheckpointMissing, match="box-a"):
        await provision_run_worktree(run, workspace_dir=tmp_path / "missing", machine_id="box-b")

    assert calls["restore"] == [] and calls["add"] == []


async def test_the_machine_that_holds_the_tree_just_keeps_working(calls, tmp_path: Path) -> None:
    present = tmp_path / "wt"
    (present / ".git").mkdir(parents=True)
    run = _run(**{CHECKPOINT_KEY: {"sha": "old", "machine": "box-a", "stale": True}})

    await provision_run_worktree(run, workspace_dir=present, machine_id="box-a")

    assert calls["restore"] == [] and calls["add"] == [run.id]


async def test_a_single_box_provisions_exactly_as_before(calls, tmp_path: Path) -> None:
    run = _run(**{CHECKPOINT_KEY: {"sha": "x", "machine": "box-a", "stale": True}})

    await provision_run_worktree(run, workspace_dir=tmp_path / "missing", machine_id="")

    assert calls["restore"] == [] and calls["add"] == [run.id]


# --- the wiring --------------------------------------------------------------------------------


def test_every_release_checkpoints_first() -> None:
    """Checkpoint BEFORE the claim is given up, at each of the three release points."""
    import inspect

    from backend.workflow.infrastructure.workers.agent_worker import AgentWorker

    for name in ("_clear_claim", "_release_claim_to_open", "_escalate_drive_failure"):
        src = "".join(inspect.getsource(getattr(AgentWorker, name)).split())
        call = "awaitself._checkpoint_before_release("
        assert call in src, name
        assert src.index(call) < src.index("claimed_by=None"), name


def test_the_product_provisioner_goes_through_the_restore() -> None:
    import inspect

    from backend.workflow.application.runtime import workspace_provisioning

    src = inspect.getsource(workspace_provisioning._product_workspace_provisioner)
    assert "provision_run_worktree(" in src
    assert "add_run_worktree(" not in src, "a direct add would branch fresh from main"


# --- a run that comes back ---------------------------------------------------------------------


async def test_a_tree_older_than_the_checkpoint_is_replaced_not_kept(
    calls, tmp_path: Path, monkeypatch
) -> None:
    """A→B→A: A still holds its tree from before the move, but B saved newer work since."""
    discarded: list[uuid.UUID] = []

    async def _discard(pid: uuid.UUID, rid: uuid.UUID) -> None:
        discarded.append(rid)

    monkeypatch.setattr(run_checkpoint, "discard_local_run_worktree", _discard)
    present = tmp_path / "wt"
    (present / ".git").mkdir(parents=True)
    run = _run(**{CHECKPOINT_KEY: {"sha": "newer", "machine": "box-b"}})

    await provision_run_worktree(run, workspace_dir=present, machine_id="box-a")

    assert discarded == [run.id] and calls["restore"] == [run.id] and calls["add"] == [run.id]


async def test_a_stale_checkpoint_elsewhere_refuses_even_with_an_old_tree_here(
    calls, tmp_path: Path
) -> None:
    present = tmp_path / "wt"
    (present / ".git").mkdir(parents=True)
    run = _run(**{CHECKPOINT_KEY: {"sha": "x", "machine": "box-b", "stale": True}})

    with pytest.raises(RunCheckpointMissing, match="box-b"):
        await provision_run_worktree(run, workspace_dir=present, machine_id="box-a")

    assert calls["add"] == [], "continuing on the older tree would drop box-b's work"
