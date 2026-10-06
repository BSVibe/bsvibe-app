"""#1113 — the run cap and the token budget, read where webhook work comes in.

Every issue / PR / comment on a bound GitHub repo becomes work, on purpose
(형님, 2026-10-06). But both limits were only read at the founder-direct doors
(``api/v1/messages.py``, ``mcp/tools/direct_tools.py``), so a webhook ran
outside them — the leak :mod:`~backend.workflow.application.run_caps` foresaw:
"the gate belongs in intake, with a refusal the founder can see".

형님 ruled what each limit does to work nobody typed into BSVibe:

* **monthly token budget** spent → REFUSED. Waiting would not help until the
  month turns, so the founder is told now (a ``needs_you`` notification).
* **concurrent-run cap** full → HELD. The trigger stays parked and starts on
  its own when a slot frees — a cap measures what runs at once, not what may
  run at all.

Only webhook triggers are gated here. A founder-direct submission already met
both limits at its door (with a 429 the PWA renders); schedules and next-step
spawns stay outside on purpose (see ``run_caps``).
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.application.run_caps import count_held_runs, load_run_cap
from backend.workflow.application.workspace_token_budget import (
    TokenBudgetReached,
    enforce_workspace_token_budget,
)
from backend.workflow.infrastructure.intake.db import RequestRow, RequestStatus


class IntakeVerdict(StrEnum):
    ADMIT = "admit"
    HOLD = "hold"
    REFUSE = "refuse"


async def room_for_new_work(session: AsyncSession, workspace_id: uuid.UUID) -> int | None:
    """How many more runs this workspace may start now — ``None`` when uncapped.

    Counts the runs it holds AND the Requests already admitted but not yet a run
    (``open``): without them one tick could admit a whole backlog into a single
    free slot.
    """
    limit = await load_run_cap(session, workspace_id)
    if limit is None:
        return None
    pending = await session.scalar(
        select(func.count())
        .select_from(RequestRow)
        .where(RequestRow.workspace_id == workspace_id, RequestRow.status == RequestStatus.OPEN)
    )
    return max(0, limit - await count_held_runs(session, workspace_id) - int(pending or 0))


async def gate_webhook_work(session: AsyncSession, workspace_id: uuid.UUID) -> IntakeVerdict:
    """Admit, hold or refuse one webhook trigger. The budget is read first: work
    that would be refused anyway must not wait for a slot first."""
    try:
        await enforce_workspace_token_budget(session, workspace_id=workspace_id)
    except TokenBudgetReached:
        return IntakeVerdict.REFUSE
    room = await room_for_new_work(session, workspace_id)
    if room is not None and room <= 0:
        return IntakeVerdict.HOLD
    return IntakeVerdict.ADMIT


__all__ = ["IntakeVerdict", "gate_webhook_work", "room_for_new_work"]
