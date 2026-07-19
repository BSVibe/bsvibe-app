"""IntakeWorker — drain TriggerEvents into Requests.

Workflow §12.5 #8 (Bundle G — Workers). DB-polling implementation (not
Redis Streams) — pulls :class:`TriggerEventRow` rows that have no paired
:class:`RequestRow` yet, claims them via row-update, and mints the
matching ``Request`` (status ``OPEN``) so the :class:`AgentWorker` can
pick it up.

The Redis Streams variant (consumer-group + XACK) remains a TODO — for
Phase 1 the DB-polling path is simpler to reason about and
integration-test, mirroring :mod:`backend.workflow.infrastructure.workers.agent_worker`. A
TriggerEvent is "drained" exactly once because the unprocessed query is
``NOT EXISTS (request with this trigger_event_id)``; once the Request is
committed the event no longer matches.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from backend.config import Settings, get_settings
from backend.notifications.emit import emit_notification
from backend.workers.base import BaseWorker
from backend.workers.emit import STREAM_AGENT, emit_stream_notification
from backend.workflow.application.stages.intake import (
    RECEIVE_FILTERED_KEY,
    filtered_out_record,
    receive,
)
from backend.workflow.channels import TRIGGER_EVENTS
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.repositories import (
    SqlAlchemyIdempotencyRepository,
    SqlAlchemyRequestRepository,
)

logger = structlog.get_logger(__name__)

# Notifier N3 — ``triggered`` fires only for AUTONOMOUS / external-origin triggers
# ("밖에서 뭔가 들어와서 일이 시작됨"): a WEBHOOK (an outside system pushed work in) or
# a SCHEDULE tick (BSVibe started work on its own). A DIRECT trigger is
# founder-initiated (they started it — no need to tell them), and a
# DECISION_RESOLUTION is a founder resuming an existing run, not a new arrival —
# both are deliberately excluded.
_TRIGGERED_KINDS: frozenset[TriggerKind] = frozenset({TriggerKind.WEBHOOK, TriggerKind.SCHEDULE})

#: Deterministic (no-LLM) founder-facing text for the ``triggered`` notification.
_TRIGGERED_TITLE = "New work came in"
#: The founder lands on the Brief (there is no run yet at intake time — the
#: Request is minted here; the AgentWorker opens the run downstream).
_TRIGGERED_LINK = "/brief"


@dataclass(slots=True)
class IntakeWorkerConfig:
    batch_size: int = 50
    poll_interval_s: float = 5.0


class IntakeWorker(BaseWorker):
    """DB-polling worker that turns un-drained TriggerEvents into Requests.

    Doubles as the *producer* for the ``agent`` stream: each minted Request is
    a row the :class:`~backend.workflow.infrastructure.workers.agent_worker.AgentWorker` would poll, so
    when ``worker_mode="redis_streams"`` the worker ALSO emits a notification
    (best-effort, soft-fail) to wake the agent consumer immediately. The DB row
    stays the source of truth — emission only happens AFTER the commit, and a
    Redis hiccup never breaks the drain (DB-polling remains the safety net).
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: IntakeWorkerConfig | None = None,
        redis_client: object | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._cfg = config or IntakeWorkerConfig()
        super().__init__(name="intake_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        # Optional — only set in redis_streams mode. None keeps the existing
        # DB-polling behaviour (no emission), so every existing caller/test is
        # unaffected.
        self._redis_client = redis_client
        self._settings = settings or get_settings()

    async def _tick(self) -> int:
        return await self.drain_once()

    async def drain_once(self) -> int:
        """Drain one batch of un-drained TriggerEvents into Requests.

        Returns the count of trigger rows PROCESSED (not Requests created):
        a filter-rejected trigger still counts as drained — the row has been
        evaluated and marked, so the next drain pass won't reprocess it. The
        Request count is the subset that pass Receive.

        Idempotency on the filter-rejected path comes from stamping the
        ``RECEIVE_FILTERED_KEY`` audit record onto the trigger row's payload;
        the ``_claim_batch`` ``NOT EXISTS (Request)`` filter alone would
        otherwise loop forever on a filtered trigger.
        """
        count = 0
        emitted_workspace_ids: list[str] = []
        async with self._session_factory() as session:
            async for trig in self._claim_batch(session):
                outcome = await receive(session, trig)
                if outcome.filtered_out:
                    # Mark the trigger row so it isn't reprocessed forever,
                    # and so an operator can audit "trigger landed but
                    # rejected by which filter".
                    trig.payload = {
                        **(trig.payload or {}),
                        RECEIVE_FILTERED_KEY: filtered_out_record(
                            filters={},
                            reason=outcome.reason or "filter_rejected",
                        ),
                    }
                    flag_modified(trig, "payload")
                    logger.info(
                        "intake_worker_trigger_filtered",
                        trigger_event_id=str(trig.id),
                        workspace_id=str(trig.workspace_id),
                        source=trig.source,
                        reason=outcome.reason,
                    )
                    count += 1
                    continue

                now = datetime.now(tz=UTC)
                # L-P1: propagate product_id from the trigger (set either by
                # ``receive()`` resolving a webhook's ResourceBinding, or by
                # the direct path resolving the founder's selected product).
                # The Request row carries it forward so AgentRunner can mint
                # the ExecutionRun with the same binding — no more NULL run.
                request_id = uuid.uuid4()
                request_repo = SqlAlchemyRequestRepository(session)
                await request_repo.enqueue(
                    RequestRow(
                        id=request_id,
                        workspace_id=trig.workspace_id,
                        trigger_event_id=trig.id,
                        product_id=outcome.product_id,
                        status=RequestStatus.OPEN,
                        payload=dict(outcome.request_payload),
                        created_at=now,
                        updated_at=now,
                    ),
                    producer_id="worker:intake_worker",
                )
                # Notifier N3 — an autonomous/external trigger just started work.
                # Queue a ``triggered`` notification in THIS transaction (confirmed
                # iff the Request commits); a founder-direct run is excluded.
                if trig.trigger_kind in _TRIGGERED_KINDS:
                    await emit_notification(
                        session,
                        workspace_id=trig.workspace_id,
                        event="triggered",
                        dedupe_key=f"triggered:{request_id}",
                        payload={
                            "title": _TRIGGERED_TITLE,
                            "body": f"A {trig.source} trigger started new work.",
                            "link": _TRIGGERED_LINK,
                            "run_id": None,
                        },
                        producer_id="worker:intake_worker",
                    )
                logger.info(
                    "intake_worker_request_created",
                    trigger_event_id=str(trig.id),
                    workspace_id=str(trig.workspace_id),
                    source=trig.source,
                    product_id=(
                        str(outcome.product_id) if outcome.product_id is not None else None
                    ),
                )
                emitted_workspace_ids.append(str(trig.workspace_id))
                count += 1
            await session.commit()
        # AFTER the commit (the row is durable) emit a wake-up per new Request.
        # Gated + soft-fail inside the helper — a no-op in DB-polling mode.
        for ws_id in emitted_workspace_ids:
            await emit_stream_notification(
                self._redis_client,  # type: ignore[arg-type]  # narrowed in helper
                settings=self._settings,
                stream=STREAM_AGENT,
                fields={"workspace_id": ws_id},
            )
        return count

    async def _claim_batch(self, session: AsyncSession) -> AsyncIterator[TriggerEventRow]:
        """Yield up to ``batch_size`` TriggerEvents that have no Request yet.

        A row is *drained* when either (a) a Request row references it, OR
        (b) Receive marked it filter-rejected (``RECEIVE_FILTERED_KEY`` set
        on the payload). The (b) clause is filtered in-process rather than
        in the WHERE — JSON key-presence semantics drift across the
        Postgres/SQLite test tiers, and an extra in-process filter is cheap
        for a small batch.
        """
        repo = SqlAlchemyIdempotencyRepository(session)
        rows = await TRIGGER_EVENTS.consume(
            consumer_id="worker:intake_worker",
            claim=lambda: repo.list_undrained(limit=self._cfg.batch_size),
        )
        for r in rows:
            if (r.payload or {}).get(RECEIVE_FILTERED_KEY) is not None:
                continue
            yield r


__all__ = ["IntakeWorker", "IntakeWorkerConfig"]
