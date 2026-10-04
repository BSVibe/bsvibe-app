"""The concurrent-run cap — how many runs a workspace may HOLD at once.

The free plan's price lever (founder decision, 2026-09-03). One rule, read by
every founder-submission surface; each surface puts its own error face on it
(REST answers 429, MCP raises a ``ToolError``) exactly as
``resolve_product_for_workspace`` owns the L-P1 rule while ``_h_direct`` only
shapes the message.

**What counts.** Every run that is not terminal — ``open``, ``running`` and,
decisively, ``review_ready``. A run awaiting review is a run holding a
workspace; that is why the reaper deliberately leaves it alone. Prod measured
103 non-terminal runs across two workspaces and *all 103* were ``review_ready``
(73/73 and 30/30, zero in flight), so counting only in-flight work would have
priced nothing at all. The set is written as "not terminal" rather than as a
list of live statuses so a status added to the enum later counts by default.

**One review_ready is not counted** (#1109): a GitHub-delivered run whose PR is
open, waiting for the merge (``payload["awaiting_merge"]``). Its work is done
and out of BSVibe's hands — counting it would let three open PRs lock the
founder out of starting anything until someone merges them.

**What is not counted.** Submissions that have not become runs yet — a
TriggerEvent or Request still walking the intake chain. A burst of submits can
therefore overshoot the cap by however many the workers have not claimed. That
is deliberate: those runs then exist, so the *next* submit is refused, and the
alternative (counting three tables at every submit) buys precision against a
burst no human types while risking a wrong count that locks a founder out.

**What is not gated at all.** Runs born WITHOUT a founder submission — a
connector webhook, a schedule tick, a multi-step frame spawning its next step.
None of them pass through here, so a free workspace can exceed its cap by those
routes. Left open on purpose: dropping an inbound webhook on the floor loses
the founder's event with nowhere to say so, and refusing step 2 of a plan would
strand step 1's output half-finished. They are *counted*, so they do consume
the budget the founder's next submission is measured against — the cap governs
what a founder may START, not what the system may FINISH. If that becomes a
paid-tier leak, the gate belongs in intake, with a refusal the founder can see
— not silently in a worker.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.workspaces_db import DEFAULT_MAX_CONCURRENT_RUNS, WorkspaceRow
from backend.workflow.infrastructure.db import TERMINAL_RUN_STATUSES, ExecutionRun, RunStatus

logger = structlog.get_logger(__name__)


class RunCapReached(Exception):
    """Raised when a workspace already holds its whole run budget.

    Carries the numbers rather than a sentence: each surface writes its own
    (localized) message, and the PWA must not hardcode a limit that differs
    per workspace.
    """

    def __init__(self, *, limit: int, held: int) -> None:
        super().__init__(f"workspace holds {held} of {limit} concurrent runs")
        self.limit = limit
        self.held = held


async def load_run_cap(session: AsyncSession, workspace_id: uuid.UUID) -> int | None:
    """The workspace's concurrent-run budget. ``None`` = uncapped.

    A missing workspace row falls back to the free default rather than to
    "uncapped": an unknown workspace must not be the cheapest way past the
    gate. A cap of ``0`` is honoured as written (it refuses everything) —
    silently rewriting it to the default would hide a deliberate suspension.
    """
    row = await session.get(WorkspaceRow, workspace_id)
    if row is None:
        logger.warning("run_cap_workspace_missing", workspace_id=str(workspace_id))
        return DEFAULT_MAX_CONCURRENT_RUNS
    return row.max_concurrent_runs


async def count_held_runs(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    """How many non-terminal runs this workspace currently holds (less those awaiting a merge)."""
    held = await session.scalar(
        select(func.count())
        .select_from(ExecutionRun)
        .where(
            ExecutionRun.workspace_id == workspace_id,
            ExecutionRun.status.not_in(tuple(TERMINAL_RUN_STATUSES)),
        )
    )
    return int(held or 0) - await _count_awaiting_merge(session, workspace_id)


async def _count_awaiting_merge(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    """REVIEW_READY runs waiting on their PR's merge (#1109). Read in Python: the
    marker is a JSON payload key, and JSON-path predicates differ between
    Postgres and SQLite; the review_ready set per workspace is small."""
    from backend.workflow.application.run_delivery_resolution import (  # noqa: PLC0415
        AWAITING_MERGE_KEY,
    )

    payloads = await session.scalars(
        select(ExecutionRun.payload).where(
            ExecutionRun.workspace_id == workspace_id,
            ExecutionRun.status == RunStatus.REVIEW_READY,
        )
    )
    return sum(1 for p in payloads if isinstance(p, dict) and AWAITING_MERGE_KEY in p)


async def enforce_run_cap(session: AsyncSession, *, workspace_id: uuid.UUID) -> None:
    """Raise :class:`RunCapReached` when this workspace has no budget left.

    Called at the founder-submission door, where a refusal has a screen to
    appear on. The worker that actually mints the run is deliberately NOT the
    gate: a run silently withheld in a background worker teaches the founder
    nothing and sells nothing.
    """
    limit = await load_run_cap(session, workspace_id)
    if limit is None:
        return
    held = await count_held_runs(session, workspace_id)
    if held < limit:
        return
    logger.info(
        "run_cap_reached",
        workspace_id=str(workspace_id),
        limit=limit,
        held=held,
    )
    raise RunCapReached(limit=limit, held=held)


__all__ = [
    "DEFAULT_MAX_CONCURRENT_RUNS",
    "RunCapReached",
    "count_held_runs",
    "enforce_run_cap",
    "load_run_cap",
]
