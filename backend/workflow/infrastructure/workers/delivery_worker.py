"""DeliveryWorker — drain shipped DeliveryEventRow rows into the dispatcher.

Workflow §12.5 #8 (Bundle G — Workers). The orchestrator writes a
DeliveryEventRow whenever a Deliverable lands in a ``shipped`` state.
This worker:

1. Polls ``delivery_events`` for unprocessed rows under a row-level
   ``SELECT … FOR UPDATE SKIP LOCKED`` claim (Lift J / v8 §11.5) — two
   server instances drain the SAME queue concurrently and the lock hint
   guarantees each row is claimed by exactly one worker. The PG lock
   releases at transaction commit (the same transaction that DELETEs
   the row). On SQLite the hint is a dialect no-op (the existing
   single-server tests still pass), but production PG honours it.
2. Consults the deliverable's workspace Safe Mode (Workflow §10.5). When
   ``workspaces.safe_mode`` is True the delivery is **enqueued** into the
   :class:`backend.workflow.application.safe_mode_queue.SafeModeQueue` (status
   ``pending``) instead of dispatching — the founder approves/denies via
   the ``/api/v1/safemode`` routes, and approval re-uses the *same*
   :func:`dispatch_delivery` helper this worker calls. When Safe Mode is
   off (or no workspace row exists — e.g. an unseeded test workspace) the
   delivery dispatches straight out exactly as before.
3. Hands each event to a caller-supplied :class:`PluginDispatchAdapter`
   so the test sink + the real PluginRunner share one shape.

DB-polling, not Redis Streams. Same justification as AgentWorker.

Lift M3 (v8 §20.4 Pattern C audit, 2026-06-02) — **SRP-clean, skipped.**
Pattern C = worker file bundling config + business logic + poll-loop
boilerplate. The poll-loop shell is already extracted to
:class:`~backend.workers.base.BaseWorker` (DeliveryWorker overrides
``_tick`` only). The config dataclass (:class:`DeliveryWorkerConfig`)
is a constructor input; the :class:`PluginDispatchAdapter` Protocol is
a narrow port the worker consumes (port defined where used — *not* a
Protocol + concrete cohabit, which would be Pattern D). Module-level
helpers (``_workspace_safe_mode``, ``_run_output_mode``,
``resolve_output_mode_gate``, ``extract_compensation_handles``,
``persist_compensation_handles``, ``dispatch_delivery``,
``build_delivery_claim_stmt``) all serve the single delivery-drain
concern and are tested individually. No split needed.

Multi-server safety (Lift J / v8 §11.5)
---------------------------------------

The claim site is :func:`build_delivery_claim_stmt` — a SELECT with
``FOR UPDATE SKIP LOCKED``. Idempotence is naturally provided by the
DELETE-on-success at the end of :meth:`DeliveryWorker.drain_once`: an
event row is either claimed + delivered + deleted (the happy path) or
left in place for the next tick (any failure path). State-transition
side effects (Deliverable updates) inside :func:`dispatch_delivery` are
themselves idempotent against the Deliverable row — re-dispatching the
same deliverable in a Safe Mode approval flow runs the same code path
and the per-Deliverable compensation_handle append is safe to repeat.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from sqlalchemy import Select, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import WorkspaceRow
from backend.shared.wire_kinds import SCHEDULE_KIND_PRODUCT_TICK
from backend.workers.base import BaseWorker
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.channels import DELIVERY_EVENTS
from backend.workflow.domain.delivery import FOUNDER_APPROVED_KEY, ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import Deliverable
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow

logger = structlog.get_logger(__name__)

# PT3 — run ``payload["kind"]`` values that mark an AUTONOMOUS-origin run (BSVibe
# decided + did the work with no founder instruction). Such deliverables ALWAYS
# route through Safe Mode so the founder's approval is the sole output control,
# regardless of the workspace flag or the binding output_mode. The kind is the
# schedule wire contract carried on the payload — single source of truth is
# ``backend.shared.wire_kinds.SCHEDULE_KIND_PRODUCT_TICK`` (a neutral leaf both
# Schedule and Workflow import, so neither context depends on the other).
_AUTONOMOUS_ORIGIN_KINDS: frozenset[str] = frozenset({SCHEDULE_KIND_PRODUCT_TICK})


class PluginDispatchAdapter(Protocol):
    """Adapter the worker calls to actually deliver."""

    async def dispatch(
        self,
        *,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
        artifact_type: str,
        plugins: Iterable[object] = (),
        context: object = None,
        event: object = None,
    ) -> DeliveryResult: ...


async def _workspace_safe_mode(session: AsyncSession, workspace_id: uuid.UUID) -> bool:
    """Return the workspace's Safe Mode flag.

    The workspace flag is the GLOBAL OVERRIDE (D3): when on, every delivery
    queues regardless of the per-Resource ``output_mode``. A missing workspace
    row defaults to ``False`` (no override) so an unseeded test workspace falls
    through to the per-Run ``output_mode`` decision / today's direct dispatch.
    """
    row = await session.get(WorkspaceRow, workspace_id)
    return bool(row.safe_mode) if row is not None else False


def resolve_output_mode_gate(
    *,
    workspace_safe_mode: bool,
    output_mode: str | None,
    autonomous_origin: bool = False,
    schedule_direct: bool = False,
) -> bool:
    """Decide whether a delivery must be QUEUED (``True``) or delivered (``False``).

    D3 / Synthesis §11 / Workflow §10.5 — the Safe Mode decision is keyed to the
    triggering Resource's ``output_mode`` and applied per-Run, with the workspace
    flag as a global override. Precedence:

    1. ``workspace_safe_mode`` (global override) — when on, ALWAYS queue.
    2. ``autonomous_origin`` (PT3) — an autonomous tick-origin run ALWAYS queues:
       BSVibe decided + did the work, so the founder's Safe Mode approval is the
       sole output control (independent of the flag or the binding output_mode).
    3. else the Resource's ``output_mode``: ``"safe"`` → queue, ``"direct"`` →
       deliver.
    4. else (no resolved ``output_mode`` — e.g. a founder-direct run with no
       binding) → deliver. With the override off this matches today's behavior,
       so a Resource with no explicit ``output_mode`` does not regress.
    """
    # PT3 first: BSVibe deciding the work itself always waits — nothing outranks it.
    if autonomous_origin:
        return True
    # #1072 — the founder marked THIS schedule "direct" when writing it; that is
    # the approval, so the workspace-wide override does not ask again.
    if schedule_direct:
        return False
    if workspace_safe_mode:
        return True
    if output_mode == "safe":
        return True
    # "direct" or None (no binding / no explicit mode) → deliver directly.
    return False


async def _run_output_mode(session: AsyncSession, run_id: uuid.UUID | None) -> str | None:
    """Resolve the triggering Resource's ``output_mode`` for a Run.

    A Run learns its triggering Resource via ``ExecutionRun.payload["binding_id"]``
    — written by the Receive stage onto the Request payload and propagated onto
    the run payload by :meth:`AgentRunner.open_run`. We load the run, read that
    ``binding_id``, and return the binding's ``output_mode`` (``"safe"`` /
    ``"direct"``). Returns ``None`` when there is no run_id, no binding_id, a
    malformed id, or no matching binding — every degraded case falls back to the
    workspace-flag behavior in :func:`resolve_output_mode_gate` (no regression).
    """
    if run_id is None:
        return None
    # Local imports keep the cross-domain dependency off module import time.
    from backend.identity.workspaces_db import ResourceBindingRow  # noqa: PLC0415
    from backend.workflow.infrastructure.db import ExecutionRun  # noqa: PLC0415

    run = await session.get(ExecutionRun, run_id)
    if run is None:
        return None
    payload = run.payload if isinstance(run.payload, dict) else {}
    binding_id_raw = payload.get("binding_id")
    if not isinstance(binding_id_raw, str):
        return None
    try:
        binding_id = uuid.UUID(binding_id_raw)
    except ValueError:
        return None
    binding = await session.get(ResourceBindingRow, binding_id)
    if binding is None:
        return None
    return binding.output_mode


async def _run_autonomous_origin(session: AsyncSession, run_id: uuid.UUID | None) -> bool:
    """PT3 — is this Run an AUTONOMOUS-origin run (e.g. a ``product_tick`` tick)?

    ``AgentRunner.open_run`` propagates the trigger ``kind`` from the Request onto
    ``ExecutionRun.payload["kind"]`` (the same seam as ``binding_id``). A run
    whose ``kind`` is in :data:`_AUTONOMOUS_ORIGIN_KINDS` must always route its
    deliverable through Safe Mode. Every degraded case (no run_id, missing run,
    non-dict payload, absent/unknown kind) returns ``False`` — no regression for
    founder-driven runs.
    """
    if run_id is None:
        return False
    from backend.workflow.infrastructure.db import ExecutionRun  # noqa: PLC0415

    run = await session.get(ExecutionRun, run_id)
    if run is None:
        return False
    payload = run.payload if isinstance(run.payload, dict) else {}
    return payload.get("kind") in _AUTONOMOUS_ORIGIN_KINDS


async def _run_schedule_direct(session: AsyncSession, run_id: uuid.UUID | None) -> bool:
    """#1072 — was this run fired by a schedule the founder marked ``direct``?"""
    if run_id is None:
        return False
    from backend.workflow.infrastructure.db import ExecutionRun  # noqa: PLC0415

    run = await session.get(ExecutionRun, run_id)
    payload = run.payload if run is not None and isinstance(run.payload, dict) else {}
    return payload.get("schedule_output_mode") == "direct"


def extract_compensation_handles(
    actions: list[ActionResult],
) -> list[dict[str, Any]]:
    """B12b — pull ``compensation_handle`` from successful actions.

    Each :class:`ActionResult.action` is shaped ``"<plugin>:outbound:<artifact>"``
    (see :class:`backend.workflow.application.delivery.dispatcher.DeliveryDispatcher` and
    :class:`backend.workflow.application.delivery.connector_dispatch.ConnectorDeliveryAdapter`). A
    successful action whose ``output`` carries ``compensation_handle`` (a
    plugin-private revert token — Workflow §3.1) produces one entry:
    ``{"plugin": "<name>", "artifact_type": "<type>", "handle": {...}}``.
    Failed actions and successful actions WITHOUT a handle are skipped — the
    retract endpoint reads this list and 400s an empty one rather than
    inventing fake entries.
    """
    entries: list[dict[str, Any]] = []
    for action in actions:
        if not action.succeeded or not action.output:
            continue
        handle = action.output.get("compensation_handle")
        if not isinstance(handle, dict) or not handle:
            continue
        plugin_name, _, rest = action.action.partition(":outbound:")
        if not plugin_name:
            continue
        artifact_type = rest or str(action.output.get("artifact_type") or "")
        entries.append(
            {
                "plugin": plugin_name,
                "artifact_type": artifact_type,
                "handle": dict(handle),
            }
        )
    return entries


async def persist_compensation_handles(
    session: AsyncSession,
    *,
    deliverable_id: uuid.UUID,
    result: DeliveryResult,
) -> int:
    """B12b — append captured handles onto the Deliverable.

    Workflow §1.2 + §3.1 + §9. Reads every successful action whose ``output``
    carries a ``compensation_handle`` (plugin-private revert token) and
    persists ``{"plugin", "artifact_type", "handle"}`` entries onto the
    Deliverable's ``compensation_handles`` column. The row may already carry
    handles from a prior dispatch of the same deliverable (Safe Mode approve
    re-runs the same code path); append rather than overwrite so re-dispatches
    keep accumulating. A missing Deliverable (purged run) is a silent no-op —
    the dispatch already succeeded, the lost handle just means retract is
    unavailable for that target. Returns the count of entries appended.
    """
    entries = extract_compensation_handles(result.actions)
    if not entries:
        return 0
    row = await session.get(Deliverable, deliverable_id)
    if row is None:
        return 0
    existing = list(row.compensation_handles or [])
    existing.extend(entries)
    # SQLAlchemy treats list assignment as a mutation; setting the attribute
    # explicitly avoids the dirty-tracking blind spot of in-place ``.extend``.
    row.compensation_handles = existing
    await session.commit()
    return len(entries)


async def _mark_delivered(session: AsyncSession, deliverable_id: uuid.UUID) -> None:
    """Record that this deliverable went out — the FIRST time only, and committed at once.

    The fact a later moment needs: a run whose output was delivered while it was still RUNNING
    is shipped when it reaches REVIEW_READY by asking this (2026-10-19)."""
    row = await session.get(Deliverable, deliverable_id)
    if row is None or row.delivered_at is not None:
        return
    row.delivered_at = datetime.now(tz=UTC)
    await session.commit()


def _delivery_succeeded(result: DeliveryResult) -> bool:
    """True when the dispatch actually DELIVERED — no top-level error, at least one
    outbound action succeeded, and NONE failed. A failed action (above all the github one,
    which carries the code) makes the deliverable undelivered however many other connectors
    went out: it must NOT auto-resolve+ship the run."""
    return (
        result.error is None
        and any(action.succeeded for action in result.actions)
        and all(action.succeeded for action in result.actions)
    )


async def _escalate_failed_delivery(
    session: AsyncSession, deliverable_id: uuid.UUID, result: DeliveryResult
) -> None:
    """Raise ONE ``delivery_failed`` Decision for a deliverable whose dispatch had a failed
    action. The run stays where it is (``review_ready``) and the deliverable stays undelivered."""
    from backend.workflow.application.run_persistence import create_decision  # noqa: PLC0415
    from backend.workflow.infrastructure.db import ExecutionRun  # noqa: PLC0415

    deliverable = await session.get(Deliverable, deliverable_id)
    if deliverable is None or deliverable.run_id is None:
        return
    run = await session.get(ExecutionRun, deliverable.run_id)
    if run is None:
        return
    failures = [
        {"action": action.action, "error": action.error}
        for action in result.actions
        if not action.succeeded
    ]
    if not failures:
        if result.error is None:
            return  # nothing was attempted (no delivery target) -- not a failure
        failures = [{"action": "delivery", "error": result.error}]
    reason = "; ".join(f"{f['action']}: {f['error']}" for f in failures)
    await create_decision(
        session,
        run,
        None,
        kind="delivery_failed",
        payload={"deliverable_id": str(deliverable_id), "failures": failures},
        rationale=f"delivery failed, nothing was shipped: {reason}",
    )
    await session.commit()


async def dispatch_delivery(
    dispatcher: PluginDispatchAdapter,
    *,
    workspace_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    artifact_type: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    session: AsyncSession | None = None,
) -> DeliveryResult:
    """The single outbound-dispatch code path shared by the worker + approve.

    Extracted so :meth:`DeliveryWorker.drain_once` (Safe Mode off) and the
    ``POST /api/v1/safemode/{item_id}/approve`` route dispatch through one
    helper rather than duplicating the call shape.

    B12b — when ``session_factory`` is provided, any ``compensation_handle``
    returned by a successful outbound action is persisted on the Deliverable
    row so the retract endpoint can later read it (Workflow §1.2 + §3.1 + §9).
    Callers that already hold an open session (e.g. an HTTP route handler)
    should omit the factory and call :func:`persist_compensation_handles`
    directly on that session — keeps the persist step inside the caller's
    transaction.

    Run auto-resolve seam: on delivery SUCCESS this also resolves the
    originating run's pending review Decision (``human_review_required`` /
    ``verification_failed``) and ships the run — see
    :func:`~backend.workflow.application.run_delivery_resolution.auto_resolve_run_on_delivery`
    for why. This is the SINGLE seam every delivery-success path funnels
    through (the DeliveryWorker direct path via ``session_factory``; the
    REST / MCP / Telegram Safe-Mode approve handlers via ``session``), so the
    two founder-gates can no longer decouple into a run stuck ``RUNNING`` while
    its work already shipped. It is a guarded no-op when there is no such
    pending Decision, so the normal verified path is untouched; and idempotent
    (a repeat delivery of an already-shipped run does nothing).
    """
    result = await dispatcher.dispatch(
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type=artifact_type,
    )
    logger.info(
        "delivery_dispatched",
        deliverable_id=str(deliverable_id),
        workspace_id=str(workspace_id),
        actions=len(result.actions),
    )
    if not _delivery_succeeded(result):
        # Nothing shipped — never ship the run off a failed dispatch. (The
        # compensation-handle capture below is likewise a no-op on failure.)
        if session_factory is not None:
            async with session_factory() as owned:
                await persist_compensation_handles(
                    owned, deliverable_id=deliverable_id, result=result
                )
        # The founder approved this and it did not land: tell them, with the reason.
        # Never raises -- the failed result is what the caller acts on.
        try:
            if session_factory is not None:
                async with session_factory() as owned:
                    await _escalate_failed_delivery(owned, deliverable_id, result)
            elif session is not None:
                await _escalate_failed_delivery(session, deliverable_id, result)
        except Exception:  # noqa: BLE001
            logger.exception(
                "delivery_failure_decision_failed",
                deliverable_id=str(deliverable_id),
                workspace_id=str(workspace_id),
            )
        return result

    # Lazy import keeps the heavy AgentRunner graph (reached transitively by the
    # auto-resolve helper) off this worker module's import time.
    from backend.workflow.application.run_delivery_resolution import (  # noqa: PLC0415
        auto_resolve_run_on_delivery,
    )

    # The outbound already went out. Anything below is book-keeping — if it
    # raises, the caller must still see a delivered result, or the worker leaves
    # the event in place and sends the same thing again next tick.
    try:
        if session_factory is not None:
            async with session_factory() as owned:
                await _mark_delivered(owned, deliverable_id)
                await persist_compensation_handles(
                    owned,
                    deliverable_id=deliverable_id,
                    result=result,
                )
                if await auto_resolve_run_on_delivery(owned, deliverable_id=deliverable_id):
                    await owned.commit()
        elif session is not None:
            await _mark_delivered(session, deliverable_id)
            # Caller-owned session (REST / MCP / Telegram approve): resolve within
            # their transaction and commit our writes (these handlers already
            # multi-commit per request — approve, then dispatch, then persist).
            if await auto_resolve_run_on_delivery(session, deliverable_id=deliverable_id):
                await session.commit()
    except Exception:  # noqa: BLE001 — never turn a delivered send into a resend
        logger.exception(
            "delivery_post_send_bookkeeping_failed",
            deliverable_id=str(deliverable_id),
            workspace_id=str(workspace_id),
        )
    return result


def build_delivery_claim_stmt(*, batch_size: int) -> Select[tuple[DeliveryEventRow]]:
    """Lift J — multi-server safe claim of pending delivery events.

    ``FOR UPDATE SKIP LOCKED`` makes the SELECT atomic w.r.t. a second
    worker on the same DB: one server's transaction locks its claimed
    rows, the other server's SELECT skips them and picks the rest. The
    lock releases when the worker commits the batch's DELETE — exactly
    one drain pass per row, no double-dispatch.

    Extracted as a builder so the unit test can pin the rendered SQL
    carries ``FOR UPDATE SKIP LOCKED`` (the load-bearing prod guard).
    """
    return (
        select(DeliveryEventRow)
        .order_by(DeliveryEventRow.created_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


@dataclass(slots=True)
class DeliveryWorkerConfig:
    batch_size: int = 50
    poll_interval_s: float = 5.0
    #: A row that raised this many times in a row is dropped (logged at error)
    #: instead of re-claimed every tick forever. Outbound sends are not
    #: idempotent — an unbounded retry of a row whose send succeeded before the
    #: raise is a message every tick (2026-10-12).
    max_attempts: int = 5


class DeliveryWorker(BaseWorker):
    """Periodic drain of ``delivery_events`` into the plugin dispatcher."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        dispatcher: PluginDispatchAdapter,
        config: DeliveryWorkerConfig | None = None,
    ) -> None:
        self._cfg = config or DeliveryWorkerConfig()
        super().__init__(name="delivery_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        self._dispatcher = dispatcher
        #: Consecutive failures per event id. In-process: a restart resets it,
        #: which only buys a row another ``max_attempts`` — still bounded.
        self._failures: dict[uuid.UUID, int] = {}
        self._audited_shipped_awaiting_merge = False

    async def _tick(self) -> int:
        processed = await self.drain_once()
        await self.ship_delivered_runs()
        return processed

    async def ship_delivered_runs(self) -> int:
        """Ship runs whose output already went out but that nothing completed — the order of
        "delivered" and "REVIEW_READY", or a race between their sessions, must not strand one.
        Never fails the tick: the drain above is this worker's job."""
        from backend.workflow.application.run_delivery_resolution import (  # noqa: PLC0415
            find_shipped_runs_with_awaiting_merge,
            ship_delivered_review_ready_runs,
        )

        try:
            if not self._audited_shipped_awaiting_merge:
                # Once per process: surface runs a failed delivery shipped by mistake.
                self._audited_shipped_awaiting_merge = True
                async with self._session_factory() as session:
                    await find_shipped_runs_with_awaiting_merge(session)
            async with self._session_factory() as session:
                return await ship_delivered_review_ready_runs(session)
        except Exception:  # noqa: BLE001 — a sweep fault must not stop deliveries
            logger.warning("delivery_worker_ship_sweep_failed", exc_info=True)
            return 0

    async def drain_once(self) -> int:
        """Pull a batch of pending events + dispatch each. Returns count delivered.

        Lift J — uses :func:`build_delivery_claim_stmt`'s ``FOR UPDATE SKIP
        LOCKED`` to multi-server safe the claim. The lock releases when the
        batch DELETE commits; a second worker on the same DB never sees the
        rows this worker claimed in the meantime.
        """
        async with self._session_factory() as session:
            stmt = build_delivery_claim_stmt(batch_size=self._cfg.batch_size)

            async def _claim() -> list[DeliveryEventRow]:
                return list((await session.execute(stmt)).scalars().all())

            rows = await DELIVERY_EVENTS.consume(
                consumer_id="worker:delivery_worker",
                claim=_claim,
            )
            if not rows:
                return 0
            queue = SafeModeQueue(session)
            processed = 0
            # At-least-once (module docstring): only rows whose enqueue/dispatch
            # actually SUCCEEDED go in the delete set. A row that raised is left
            # OUT so the final DELETE leaves it in place and the next tick retries
            # it — never silently dropped. Poison-pill trade-off: a permanently
            # failing row is re-claimed every tick forever. That is the correct
            # at-least-once behavior (retry beats silent loss) — up to
            # ``max_attempts``; past that the row is dropped with an error log,
            # because an outbound that succeeded before the raise would
            # otherwise repeat every tick.
            delivered_ids: list[uuid.UUID] = []
            dropped_ids: list[uuid.UUID] = []
            for row in rows:
                # #959 — everything below belongs to exactly ONE tenant, so
                # publish it: layer 2 (the ORM auto-filter) and layer 3 (the
                # RLS GUC) both read this contextvar. Without it BOTH are
                # no-ops and these queries run unfiltered in the DB. The claim
                # above stays workspace-blind on purpose — a queue poller
                # crosses tenants. Released on the way out (including on a
                # raise), so the next claim is tenant-blind again.
                try:
                    async with workspace_session_scope(session, row.workspace_id):
                        workspace_safe_mode = await _workspace_safe_mode(session, row.workspace_id)
                        output_mode = await _run_output_mode(session, row.run_id)
                        autonomous_origin = await _run_autonomous_origin(session, row.run_id)
                        schedule_direct = await _run_schedule_direct(session, row.run_id)
                        # #1112 — the founder's "Approve & ship" click already
                        # approved this one; a second Safe Mode card would ask twice.
                        founder_approved = (row.payload or {}).get(FOUNDER_APPROVED_KEY) is True
                        if not founder_approved and resolve_output_mode_gate(
                            workspace_safe_mode=workspace_safe_mode,
                            output_mode=output_mode,
                            autonomous_origin=autonomous_origin,
                            schedule_direct=schedule_direct,
                        ):
                            # Gate says QUEUE — hold for founder approval instead of
                            # dispatching (D3: per-Run output_mode == "safe", OR the
                            # workspace global override is on). The /api/v1/safemode
                            # routes drive it the rest of the way. ``run_id`` (B12a)
                            # threads the originating run onto the queue item so the
                            # founder can approve all of a run's accumulated partial
                            # Deliver events as ONE transaction (Workflow §1.2).
                            await queue.enqueue(
                                workspace_id=row.workspace_id,
                                deliverable_id=row.deliverable_id,
                                run_id=row.run_id,
                            )
                            logger.info(
                                "delivery_worker_enqueued_safe_mode",
                                event_id=str(row.id),
                                deliverable_id=str(row.deliverable_id),
                            )
                        else:
                            await dispatch_delivery(
                                self._dispatcher,
                                workspace_id=row.workspace_id,
                                deliverable_id=row.deliverable_id,
                                artifact_type=row.artifact_type,
                                # B12b — capture compensation_handle onto the
                                # Deliverable so the retract endpoint can later
                                # revert through @p.compensate.
                                session_factory=self._session_factory,
                            )
                except Exception:  # noqa: BLE001 — record + move on
                    # Failure path: row.id NOT added to delivered_ids, so the
                    # DELETE below leaves this event in place for the next tick.
                    attempts = self._failures.get(row.id, 0) + 1
                    logger.exception(
                        "delivery_worker_dispatch_failed",
                        event_id=str(row.id),
                        deliverable_id=str(row.deliverable_id),
                        attempt=attempts,
                    )
                    if attempts >= self._cfg.max_attempts:
                        logger.error(
                            "delivery_worker_event_dropped",
                            event_id=str(row.id),
                            deliverable_id=str(row.deliverable_id),
                            run_id=str(row.run_id),
                            attempts=attempts,
                        )
                        self._failures.pop(row.id, None)
                        dropped_ids.append(row.id)
                    else:
                        self._failures[row.id] = attempts
                    continue
                self._failures.pop(row.id, None)
                delivered_ids.append(row.id)
                processed += 1
            # Scope the DELETE to ONLY the successfully-drained rows — failed rows
            # survive for retry (at-least-once). If zero succeeded, skip the DELETE
            # entirely (no rows to remove; the whole batch is left for the next
            # tick). The DELETE + commit stay one transaction with the FOR UPDATE
            # claim: succeeded rows are removed, failed rows remain committed as-is.
            if delivered_ids or dropped_ids:
                await session.execute(
                    delete(DeliveryEventRow).where(
                        DeliveryEventRow.id.in_(delivered_ids + dropped_ids)
                    )
                )
            await session.commit()
            return processed


__all__ = [
    "FOUNDER_APPROVED_KEY",
    "DeliveryWorker",
    "DeliveryWorkerConfig",
    "PluginDispatchAdapter",
    "build_delivery_claim_stmt",
    "dispatch_delivery",
    "resolve_output_mode_gate",
]
