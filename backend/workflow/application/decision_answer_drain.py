"""Apply the chat answers the inbound layer queued — 게이트 3 후속.

The other half of :mod:`backend.connectors.decision_answer_queue`. A founder taps
an answer on a ``needs_you`` card; the inbound layer validates it, writes it onto
the Decision inside the request transaction, and returns fast. This is the side
that is allowed to do the heavy part — record the answer, flip the run
``RUNNING → OPEN`` so a worker re-drives it, run ``ship`` / ``discard`` side
effects — because it runs in the ENGINE, where reaching the resolver (and through
it ``plugin.audit``) is the normal direction of dependency.

It goes through :func:`resolve_checkpoint`, the same call the PWA's Brief makes,
so there is exactly ONE chain that answers a Decision. A second implementation
would be free to drift on the parts that matter, and the founder would get
different outcomes depending on which surface they answered from.

**Draining is idempotent by clearing.** The queued answer is removed in the same
transaction that applies it, so a retried tick finds nothing rather than
resolving twice. An entry that cannot be read is dropped rather than retried: a
poison row the drain re-reads every tick is how a queue stops moving, and
dropping it leaves the Decision answerable again — from the phone or the Brief.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.decision_answer_queue import (
    QUEUED_ANSWER_KEY,
    clear_queued_answer,
    queued_answer,
)
from backend.workflow.application.checkpoint_resolution import (
    CheckpointNotFound,
    resolve_checkpoint,
)
from backend.workflow.infrastructure.db import Decision, DecisionStatus

logger = structlog.get_logger(__name__)

#: Decisions applied per tick. Small because each one resumes a run and may ship
#: a deliverable — a big batch would hold the session open across all of them.
_BATCH = 20


async def drain_queued_answers(session: AsyncSession, *, limit: int = _BATCH) -> int:
    """Apply every queued chat answer on a PENDING Decision. Returns the count.

    Scans PENDING Decisions and acts ONLY on those carrying a queued answer —
    a scan that acted on all of them would settle the founder's whole queue on
    its first tick.
    """
    rows = (
        (
            await session.execute(
                select(Decision).where(Decision.status == DecisionStatus.PENDING).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    applied = 0
    for decision in rows:
        if QUEUED_ANSWER_KEY not in (decision.payload or {}):
            continue
        answer = queued_answer(decision)
        if answer is None:
            # Unreadable — drop it so the queue keeps moving. The Decision stays
            # pending and answerable; nothing was decided on the founder's behalf.
            clear_queued_answer(decision)
            await session.commit()
            continue
        try:
            await resolve_checkpoint(
                session,
                workspace_id=decision.workspace_id,
                checkpoint_id=decision.id,
                answer=answer.answer,
                action_key=answer.action_key,
                actor_id=answer.actor_id,
            )
        except CheckpointNotFound:
            # Someone answered it in the Brief first. Clear and move on — the
            # founder's intent is already satisfied.
            clear_queued_answer(decision)
            await session.commit()
            continue
        except Exception:  # noqa: BLE001 — one bad Decision must not stall the rest
            logger.warning(
                "decision_answer_apply_failed",
                decision_id=str(decision.id),
                action_key=answer.action_key,
                exc_info=True,
            )
            await session.rollback()
            continue
        clear_queued_answer(decision)
        await session.commit()
        applied += 1
        logger.info(
            "decision_answer_applied",
            decision_id=str(decision.id),
            connector=answer.connector,
            action_key=answer.action_key,
        )
    return applied


__all__ = ["drain_queued_answers"]
