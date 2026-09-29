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

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.decision_answer_queue import (
    QUEUED_ANSWER_KEY,
    clear_queued_answer,
    queued_answer,
)
from backend.data.rls import cross_tenant_session_read, workspace_session_scope
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

    #959 — the scan crosses tenants and asks for it by name; each answer is then
    applied inside its own tenant's scope, where ``WITH CHECK`` accepts the
    writes. Only ids leave the scan: a rollback below expires the loaded rows,
    and re-reading one must happen under the scope, not after it.
    """
    async with cross_tenant_session_read(session):
        rows = (
            (
                await session.execute(
                    select(Decision).where(Decision.status == DecisionStatus.PENDING).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        queued = [(d.id, d.workspace_id) for d in rows if QUEUED_ANSWER_KEY in (d.payload or {})]
    applied = 0
    for decision_id, workspace_id in queued:
        async with workspace_session_scope(session, workspace_id):
            applied += await _apply_one(session, decision_id)
    return applied


async def _apply_one(session: AsyncSession, decision_id: uuid.UUID) -> int:
    """Apply one queued answer; 1 when it resolved the Decision, else 0."""
    decision = await session.get(Decision, decision_id)
    if decision is None:
        return 0
    answer = queued_answer(decision)
    if answer is None:
        # Unreadable — drop it so the queue keeps moving. The Decision stays
        # pending and answerable; nothing was decided on the founder's behalf.
        clear_queued_answer(decision)
        await session.commit()
        return 0
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
        return 0
    except Exception:  # noqa: BLE001 — one bad Decision must not stall the rest
        logger.warning(
            "decision_answer_apply_failed",
            decision_id=str(decision_id),
            action_key=answer.action_key,
            exc_info=True,
        )
        await session.rollback()
        return 0
    clear_queued_answer(decision)
    await session.commit()
    logger.info(
        "decision_answer_applied",
        decision_id=str(decision_id),
        connector=answer.connector,
        action_key=answer.action_key,
    )
    return 1


__all__ = ["drain_queued_answers"]
