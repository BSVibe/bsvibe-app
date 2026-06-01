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
from typing import Any, Protocol

import structlog
from sqlalchemy import Select, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.identity.workspaces_db import WorkspaceRow
from backend.workers.base import BaseWorker
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import Deliverable
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow

logger = structlog.get_logger(__name__)


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


def resolve_output_mode_gate(*, workspace_safe_mode: bool, output_mode: str | None) -> bool:
    """Decide whether a delivery must be QUEUED (``True``) or delivered (``False``).

    D3 / Synthesis §11 / Workflow §10.5 — the Safe Mode decision is keyed to the
    triggering Resource's ``output_mode`` and applied per-Run, with the workspace
    flag as a global override. Precedence:

    1. ``workspace_safe_mode`` (global override) — when on, ALWAYS queue.
    2. else the Resource's ``output_mode``: ``"safe"`` → queue, ``"direct"`` →
       deliver.
    3. else (no resolved ``output_mode`` — e.g. a founder-direct run with no
       binding) → deliver. With the override off this matches today's behavior,
       so a Resource with no explicit ``output_mode`` does not regress.
    """
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


async def dispatch_delivery(
    dispatcher: PluginDispatchAdapter,
    *,
    workspace_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    artifact_type: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
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
    if session_factory is not None:
        async with session_factory() as session:
            await persist_compensation_handles(
                session,
                deliverable_id=deliverable_id,
                result=result,
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

    async def _tick(self) -> int:
        return await self.drain_once()

    async def drain_once(self) -> int:
        """Pull a batch of pending events + dispatch each. Returns count delivered.

        Lift J — uses :func:`build_delivery_claim_stmt`'s ``FOR UPDATE SKIP
        LOCKED`` to multi-server safe the claim. The lock releases when the
        batch DELETE commits; a second worker on the same DB never sees the
        rows this worker claimed in the meantime.
        """
        async with self._session_factory() as session:
            stmt = build_delivery_claim_stmt(batch_size=self._cfg.batch_size)
            rows = (await session.execute(stmt)).scalars().all()
            if not rows:
                return 0
            queue = SafeModeQueue(session)
            processed = 0
            for row in rows:
                try:
                    workspace_safe_mode = await _workspace_safe_mode(session, row.workspace_id)
                    output_mode = await _run_output_mode(session, row.run_id)
                    if resolve_output_mode_gate(
                        workspace_safe_mode=workspace_safe_mode, output_mode=output_mode
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
                    logger.exception(
                        "delivery_worker_dispatch_failed",
                        event_id=str(row.id),
                        deliverable_id=str(row.deliverable_id),
                    )
                    continue
                processed += 1
            await session.execute(
                delete(DeliveryEventRow).where(DeliveryEventRow.id.in_([r.id for r in rows]))
            )
            await session.commit()
            return processed


__all__ = [
    "DeliveryWorker",
    "DeliveryWorkerConfig",
    "PluginDispatchAdapter",
    "build_delivery_claim_stmt",
    "dispatch_delivery",
    "resolve_output_mode_gate",
]
