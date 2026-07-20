"""Worker registry + Redis-Streams consumer wiring (§17.2a slice).

The middle layer between the runtime construction (agent/settle/delivery
factories) and the process lifecycle (signal handlers / boot path):

* :class:`WorkerRuntime` — owns the worker set + its shared engine; runs
  every worker concurrently until stopped.
* :func:`build_worker_runtime` — constructs the full worker set against
  one shared session factory (DB-polling default).
* :func:`check_executor_dispatch_health` — B14 operator liveness probe;
  loud-at-startup warning when an executor pool is configured but Redis
  is not.
* :class:`StreamConsumerBinding` + :func:`build_stream_consumers` +
  :func:`run_stream_consumers` — opt-in Redis-Streams consumer wiring
  (``worker_mode="redis_streams"``). Each consumer loops XREADGROUP →
  the worker's OWN single-tick method → XACK; Redis is only a different
  *trigger* for the same DB-driven logic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings, get_settings
from backend.knowledge.infrastructure.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
    build_garden_promoter_factory,
)
from backend.router.accounts.models import ModelAccount
from backend.router.accounts.predicates import EXECUTOR_PROVIDER
from backend.schedule.infrastructure.db_poll_runner import build_db_poll_schedule_runner
from backend.schedule.infrastructure.workers.schedule_worker import (
    ScheduleWorker,
    ScheduleWorkerConfig,
)
from backend.workers.base import BaseWorker
from backend.workers.relays import build_relay
from backend.workers.stream_keys import STREAM_KEY_BY_CONSUMER
from backend.workers.streams import RedisStreamConsumer, StreamHandler
from backend.workflow.application.runtime.settle_runtime import (
    build_concept_framer,
    build_note_embed_hook,
    build_reconcile_hook,
    build_settle_entity_extractor_factory,
)
from backend.workflow.application.safe_mode_expiry import SafeModeExpirySweepRunner
from backend.workflow.infrastructure.workers.agent_worker import (
    AgentExecutionDeps,
    AgentWorker,
)
from backend.workflow.infrastructure.workers.daily_brief_worker import DailyBriefWorker
from backend.workflow.infrastructure.workers.delivery_worker import (
    DeliveryWorker,
    PluginDispatchAdapter,
)
from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker
from backend.workflow.infrastructure.workers.notify_worker import NotifySender, NotifyWorker
from backend.workflow.infrastructure.workers.relay_worker import RelayWorker
from plugin.audit.retention_sweep import AuditRetentionSweepRunner

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class WorkerRuntime:
    """Owns the worker set + its shared engine; runs them until stopped."""

    workers: list[BaseWorker]
    _stop: asyncio.Event

    async def run_forever(self) -> None:
        """Start every worker, then block until :meth:`request_stop` / a signal."""
        for worker in self.workers:
            await worker.start()
        logger.info("worker_runtime_started", workers=[w._name for w in self.workers])
        try:
            await self._stop.wait()
        finally:
            await self.shutdown()

    def request_stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        """Stop every worker (graceful — drains the in-flight tick first)."""
        for worker in self.workers:
            await worker.stop()
        logger.info("worker_runtime_stopped")


async def check_executor_dispatch_health(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    redis_url: str,
) -> dict[str, Any]:
    """B14 — operator liveness probe for executor dispatch readiness.

    The :class:`~backend.dispatch.adapter.ExecutorAdapter` dispatches a chat
    task to a CLI worker by XADDing onto a Redis Stream. When ``settings.redis_url``
    is empty the adapter raises a ``no_executor_dispatch_transport``
    :class:`Decision` at run time — a correct, non-silent failure mode, but one
    that only surfaces AFTER an executor run has been minted. An operator that
    has configured executor workers (one or more active
    ``provider='executor'`` :class:`ModelAccount` rows) with no Redis URL set
    will see every executor run fail this way.

    This helper makes the misconfiguration **loud at startup**: it counts
    active executor accounts across all workspaces and, when the count is
    positive AND ``redis_url`` is empty, emits a structured
    ``executor_dispatch_no_redis`` WARNING that points operators at the
    ``BSVIBE_REDIS_URL`` env var. It NEVER crashes — preserves the existing
    runtime contract; it only adds visibility.

    Returns a dict (``healthy``, ``executor_account_count``,
    ``redis_configured``) so a future CLI ``health`` command / smoke probe can
    surface the same signal without re-grepping logs.
    """
    redis_configured = bool(redis_url)
    async with session_factory() as session:
        result = await session.execute(
            select(func.count())
            .select_from(ModelAccount)
            .where(
                ModelAccount.provider == EXECUTOR_PROVIDER,
                ModelAccount.is_active.is_(True),
            )
        )
    count = int(result.scalar() or 0)
    healthy = redis_configured or count == 0
    if not healthy:
        logger.warning(
            "executor_dispatch_no_redis",
            executor_account_count=count,
            hint=(
                "executor accounts are active but BSVIBE_REDIS_URL is empty — "
                "every executor run will raise a 'no_executor_dispatch_transport' "
                "Decision; set BSVIBE_REDIS_URL (e.g. redis://localhost:6387/0) "
                "to enable worker dispatch"
            ),
        )
    return {
        "healthy": healthy,
        "executor_account_count": count,
        "redis_configured": redis_configured,
    }


def build_worker_runtime(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    execution: AgentExecutionDeps,
    delivery_adapter: PluginDispatchAdapter,
    notify_sender: NotifySender,
    settings: Settings | None = None,
    redis_client: Any = None,
) -> WorkerRuntime:
    """Construct the full worker set against one shared session factory.

    ``redis_client`` is wired into the producer-side workers (the IntakeWorker
    emits an ``agent`` notification per minted Request) ONLY in
    ``worker_mode="redis_streams"``; ``None`` (the default) keeps the pure
    DB-polling behaviour (emission is gated + soft-fail). ``notify_sender`` is
    the Notifier-N2 push port the NotifyWorker drains the outbox through."""
    settings = settings or get_settings()
    workers: list[BaseWorker] = [
        IntakeWorker(
            session_factory=session_factory,
            redis_client=redis_client,
            settings=settings,
        ),
        AgentWorker(session_factory=session_factory, execution=execution),
        DeliveryWorker(session_factory=session_factory, dispatcher=delivery_adapter),
        NotifyWorker(session_factory=session_factory, sender=notify_sender),
        # Notifier daily_brief — per-workspace once-a-day digest producer.
        DailyBriefWorker(session_factory=session_factory),
        SettleWorker(
            session_factory=session_factory,
            sink=KnowledgeSettleSink(
                vault_root=Path(settings.knowledge_vault_root),
                # PRIMARY content-tag source: concepts from LLM-extracted entities
                # (soft-falls back to the deterministic heuristic). redis lets an
                # executor-account settle route dispatch the chat onto the stream.
                extractor_factory=build_settle_entity_extractor_factory(
                    session_factory=session_factory, settings=settings, redis=redis_client
                ),
                # v2: verified-work knowledge is AGENT-authored — the working
                # agent declares it in its verification contract (threaded onto
                # the settle payload). No post-hoc extractor; the sink writes the
                # agent's own declaration. Routine work declares none → no note.
            ),
            config=SettleWorkerConfig(default_region=settings.knowledge_default_region),
            # Close the §5 ratchet loop: promote each affected workspace's garden
            # observations into canon over the sink's vault boundary. Lift 1b —
            # a routed ConceptFramer distils each new concept body (user-routed
            # via knowledge.canonicalization; deterministic Lift 1 body on miss).
            promoter_factory=build_garden_promoter_factory(
                vault_root=Path(settings.knowledge_vault_root),
                framer_factory=build_concept_framer(
                    session_factory=session_factory, settings=settings, redis=redis_client
                ),
            ),
            # G5b — populate the pgvector note store from each absorbed note so
            # G5a's SemanticNoteRetriever has data to search. No-op until a
            # workspace configures an embedding model.
            embed_hook=build_note_embed_hook(session_factory=session_factory, settings=settings),
            # Lift 2 — after a concept-creating promote pass, embed the freshly
            # created concept body (which fires no write event in the settle
            # runtime) so it is retrievable without a manual reconcile. Gated on
            # PromotionResult.created_concepts; soft-fail; no-op until a workspace
            # configures an embedding model.
            reconcile_hook=build_reconcile_hook(session_factory=session_factory, settings=settings),
        ),
        # Config-driven relay: HttpRelay when ``audit_relay_url`` is set,
        # else the no-sink LoggingRelay default (drain + ack, no delivery).
        RelayWorker(session_factory=session_factory, relay=build_relay(settings)),
        # M1 — schedule runner. DB-polls ``workspace_schedules`` for rows where
        # ``enabled=True AND next_run_at <= now`` and fires a
        # :class:`ScheduleTrigger` on each (downstream IntakeWorker then drains
        # the new TriggerEvent into a Request).
        ScheduleWorker(
            session_factory=session_factory,
            runner=build_db_poll_schedule_runner(),
        ),
        # D3a — Safe Mode expiry sweep. A SECOND ScheduleWorker against the
        # SAME ScheduleRunnerProtocol seam but a different runner: the
        # :class:`SafeModeExpirySweepRunner` selects every PENDING/EXTENDED
        # safe_mode_queue_items row past ``expires_at`` (across ALL workspaces),
        # transitions each via :meth:`SafeModeQueue.mark_expired`, and emits ONE
        # ``safe_mode.expired`` AuditOutboxRecord per non-empty batch
        # tagged ``trigger=schedule, source=system.safe_mode_expiry``.
        ScheduleWorker(
            session_factory=session_factory,
            runner=SafeModeExpirySweepRunner(),
            name="safe_mode_expiry_worker",
            # Hourly is fine — TTLs are day-grained (90d initial + 30d
            # extensions), and a row drifting one tick past ``expires_at``
            # before the sweep catches it has no founder impact.
            config=ScheduleWorkerConfig(poll_interval_s=3600.0),
        ),
        # Lift Q1 — per-workspace audit_outbox retention sweep. A THIRD
        # :class:`ScheduleWorker` against the SAME :class:`ScheduleRunnerProtocol`
        # seam but a different runner: :class:`AuditRetentionSweepRunner` iterates
        # every workspace with a non-NULL ``audit_retention_days`` (NULL = forever,
        # the default), DELETEs ``audit_outbox`` rows past
        # ``occurred_at < now - retention_days * 1d``, and emits ONE
        # ``audit.retention.swept`` row per workspace per non-empty batch tagged
        # ``trigger=schedule, source=system.audit_retention``. Daily poll —
        # retention is day-grained; a row drifting past cutoff has no impact.
        ScheduleWorker(
            session_factory=session_factory,
            runner=AuditRetentionSweepRunner(),
            name="audit_retention_sweep_worker",
            config=ScheduleWorkerConfig(poll_interval_s=86400.0),
        ),
    ]
    return WorkerRuntime(workers=workers, _stop=asyncio.Event())


# ---------------------------------------------------------------------------
# Redis Streams consumer wiring (opt-in — worker_mode="redis_streams")
# ---------------------------------------------------------------------------
#
# This path is purely ADDITIVE. The DB-polling default above is UNTOUCHED. When
# ``worker_mode="redis_streams"`` the daemon drives each worker by a Redis
# Streams consumer (XREADGROUP → handler → XACK) INSTEAD of the poll loop — but
# the handler is the worker's OWN single-tick method, so no business logic is
# duplicated: Redis is only a different *trigger* for the same tick.


@dataclass(slots=True)
class StreamConsumerBinding:
    """One worker bound to its source stream + consumer group + tick handler."""

    stream_name: str
    consumer_group: str
    handler: StreamHandler


def _tick_handler(tick: Callable[[], Awaitable[int]]) -> StreamHandler:
    """Adapt a worker's no-arg single-tick method to a stream handler.

    The notification fields are intentionally ignored — the worker's tick reads
    its own source table (the DB row is the source of truth); the stream entry
    is only a wake-up."""

    async def _handle(_fields: dict[str, Any]) -> None:
        await tick()

    return _handle


def build_stream_consumers(workers: list[Any]) -> list[StreamConsumerBinding]:
    """Map known workers to their (stream, group, handler) bindings.

    * intake_worker → ``intake`` stream, handler = ``drain_once``
    * agent_worker → ``agent`` stream, handler = ``_tick`` (claim + drive)
    * delivery_worker → ``deliver`` stream, handler = ``drain_once``
    * settle_worker → ``settle`` stream, handler = ``drain_once``

    The relay_worker is intentionally OMITTED — it drains the audit outbox on
    its own cadence, not in response to a producer event, so it has no stream.
    A worker whose name is not in the mapping is skipped (not crashed).

    The worker→stream map is DERIVED from the single
    :data:`~backend.workers.stream_keys.STREAM_KEY_BY_CONSUMER` declaration, so
    this consumer side can no longer drift from the producer-side constants in
    :mod:`backend.workers.emit` (both resolve to the same typed
    :class:`~backend.workers.stream_keys.StreamKey` bindings)."""
    bindings: list[StreamConsumerBinding] = []
    for worker in workers:
        name = getattr(worker, "_name", None)
        if not isinstance(name, str):
            continue
        stream = STREAM_KEY_BY_CONSUMER.get(name)
        if stream is None:
            continue
        # agent_worker advances through claim + drive in one tick (``_tick``);
        # the queue-style workers expose a single ``drain_once``. Both reach
        # the SAME logic — preferring ``_tick`` keeps the trigger faithful to
        # the poll-loop body.
        tick = getattr(worker, "_tick", None)
        if tick is None:
            tick = worker.drain_once
        bindings.append(
            StreamConsumerBinding(
                stream_name=stream,
                consumer_group=name,
                handler=_tick_handler(tick),
            )
        )
    return bindings


async def run_stream_consumers(
    *,
    workers: list[BaseWorker],
    redis_client: Any,
    stop_event: asyncio.Event,
    consumer_name: str = "worker-1",
) -> None:
    """Run a :class:`RedisStreamConsumer` per worker binding until stopped.

    Each consumer loops XREADGROUP → the worker's own tick handler → XACK. The
    relay worker (no stream binding) keeps running on its DB-poll loop so the
    audit outbox still drains; it is started/stopped alongside the consumers."""
    consumer = RedisStreamConsumer(redis_client)
    bindings = build_stream_consumers(list(workers))
    bound_groups = {b.consumer_group for b in bindings}

    # Workers without a stream binding (relay) still poll their own source.
    poll_workers = [w for w in workers if getattr(w, "_name", None) not in bound_groups]
    for w in poll_workers:
        await w.start()

    tasks = [
        asyncio.create_task(
            consumer.consume(
                stream_name=b.stream_name,
                consumer_group=b.consumer_group,
                consumer_name=consumer_name,
                handler=b.handler,
                stop_event=stop_event,
            ),
            name=f"stream::{b.consumer_group}",
        )
        for b in bindings
    ]
    logger.info("worker_runtime_started_redis_streams", streams=sorted(bound_groups))
    try:
        await stop_event.wait()
    finally:
        for w in poll_workers:
            await w.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:  # pragma: no cover — expected on shutdown
                pass


__all__ = [
    "StreamConsumerBinding",
    "WorkerRuntime",
    "build_stream_consumers",
    "build_worker_runtime",
    "check_executor_dispatch_health",
    "run_stream_consumers",
]
