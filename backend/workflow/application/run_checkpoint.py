"""Where a run's worktree is saved off the machine, and where it comes back (#929, B).

The primitives (:func:`checkpoint_run_worktree`, :func:`restore_run_worktree`) live with the
other product-repo operations in :mod:`backend.storage.product_workspace`. This module is the
policy around them — docs/design/stateless-workers.md §2.2–2.3:

* **Before a claim is given up** (pause, back to the queue, escalation) the worktree is
  checkpointed. Before, not after: a run back in the queue can be claimed by another machine
  the moment the claim is gone.
* **The outcome is recorded on the run** (``payload["checkpoint"]``): the snapshot sha and the
  machine that holds the tree — or ``stale`` when the save failed, because the latest work then
  exists only on that machine.
* **A machine that lacks the run's tree** restores it. A run that HAS a checkpoint but cannot be
  restored, or whose checkpoint is stale, is refused (:class:`RunCheckpointMissing`) — never
  branched fresh from ``main``, which is how work used to vanish without an error. A run with
  no checkpoint is new.
* **A single-box deployment** (``BSVIBE_MACHINE_ID`` empty) does none of this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from backend.storage.product_workspace import (
    add_run_worktree,
    checkpoint_run_worktree,
    restore_run_worktree,
)
from backend.workflow.application.runtime.account_resolution import product_is_client_attach
from backend.workflow.domain.run_checkpoint import CHECKPOINT_KEY

logger = structlog.get_logger(__name__)


class RunCheckpointMissing(RuntimeError):
    """This machine cannot rebuild a run that already did work elsewhere.

    Raised instead of branching fresh from ``main`` — that silently discards the run's work. It
    surfaces as a drive failure, and past the failure bound the founder gets a Decision."""


async def checkpoint_before_release(session: Any, run: Any, *, machine_id: str) -> None:
    """Save the run's worktree off this machine and record it on ``run.payload``.

    Mutates ``run.payload`` only; the caller commits it with the claim release. Never raises:
    a failed save is recorded as ``stale`` (the caller still releases — holding the claim would
    stall the run on a machine that may be going away), and the next machine refuses to start
    over instead of quietly losing the newer work.
    """
    if not machine_id or run.product_id is None:
        return
    if session is not None and await product_is_client_attach(session, run.product_id):
        return  # the tree is on the founder's machine, not on this one
    previous = dict((run.payload or {}).get(CHECKPOINT_KEY) or {})
    try:
        sha = await checkpoint_run_worktree(run.product_id, run.id)
    except Exception:  # noqa: BLE001 — recorded as stale below; never block the release
        logger.warning("run_checkpoint_failed", run_id=str(run.id), exc_info=True)
        record = {**previous, "machine": machine_id, "stale": True}
    else:
        if sha is None:
            return  # this machine holds no tree for the run — nothing newer to record
        record = {"sha": sha, "machine": machine_id}
    run.payload = {**(run.payload or {}), CHECKPOINT_KEY: record}


async def provision_run_worktree(run: Any, *, workspace_dir: Path, machine_id: str) -> Path:
    """Make the run's worktree present on THIS machine: keep, restore, or start it."""
    checkpoint = (run.payload or {}).get(CHECKPOINT_KEY) or {}
    if machine_id and checkpoint and not (workspace_dir / ".git").exists():
        if checkpoint.get("stale"):
            raise RunCheckpointMissing(
                f"run {run.id}: its latest work was not saved off {checkpoint.get('machine')}; "
                "it can only continue there"
            )
        if not await restore_run_worktree(run.product_id, run.id):
            raise RunCheckpointMissing(
                f"run {run.id}: checkpoint {str(checkpoint.get('sha'))[:12]} from "
                f"{checkpoint.get('machine')} could not be restored"
            )
    # Idempotent for a tree that is present (kept or just restored); a fresh branch from
    # ``main`` only for a run that never had one.
    return await add_run_worktree(run.product_id, run.id)


__all__ = [
    "CHECKPOINT_KEY",
    "RunCheckpointMissing",
    "checkpoint_before_release",
    "provision_run_worktree",
]
