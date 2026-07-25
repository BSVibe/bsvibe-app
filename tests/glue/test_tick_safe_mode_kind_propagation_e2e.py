"""[P·hop] Tick → Safe-Mode kind propagation END-TO-END (PT3).

The autonomy safety boundary: an autonomous ``product_tick`` deliverable MUST be
held for founder approval — regardless of workspace ``safe_mode`` and with no
``safe`` binding — and that hinges on ``run.payload["kind"] == "product_tick"``
propagating the WHOLE way:

    ScheduleTrigger.fire (real emitter — stamps payload["kind"])
      → IntakeWorker.drain_once  (real intake — mints the Request, kind rides through)
      → AgentRunner.open_run     (real propagation — copies kind onto run.payload)
      → Deliverable + DeliveryEventRow
      → DeliveryWorker.drain_once (real gate — resolve_output_mode_gate)

The audit found a MOCK-HIDDEN blind spot: the existing PT3 tests
(``test_product_tick_planner_wiring`` / ``test_safe_mode_output_mode_gate``)
pre-seed ``payload["kind"]`` by hand, so a broken emitter→intake→open_run hop
would be invisible — the gate would still see the hand-set kind. Here NOTHING
sets ``payload["kind"]`` manually: the emitter stamps it and every downstream
hop propagates it. The gate firing therefore PROVES the kind survived the whole
chain.

How it would fail if the propagation hop were disconnected: if the emitter
stopped stamping ``kind``, or intake dropped it from the request payload, or
``open_run`` stopped copying it onto ``run.payload``, then
``_run_autonomous_origin`` would read no kind, the gate would return "deliver",
and the ``product_tick`` deliverable would be DISPATCHED instead of held — this
test's SafeModeQueue assertion (and the dispatcher-not-called assertion) turns
red. The companion instruction-run test proves the gate keys specifically on the
propagated ``product_tick`` kind, not a blanket hold.

Runs on real Postgres (seeds schedules/runs/deliverables — SQLite lies about
FK/enum types; CI is PG). Skips when no PG is configured so it never silently
"passes" on SQLite.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Register every table these rows touch on the shared Base.metadata.
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
import backend.workflow.infrastructure.delivery.db  # noqa: F401
import backend.workflow.infrastructure.intake.db  # noqa: F401
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.schedule.application.emitter import ScheduleTrigger
from backend.schedule.infrastructure.schedule_db import SCHEDULE_KIND_PRODUCT_TICK
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.domain.delivery import DeliveryResult
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
)
from backend.workflow.infrastructure.delivery.db import (
    DeliveryEventRow,
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from backend.workflow.infrastructure.intake.db import RequestRow
from backend.workflow.infrastructure.workers.delivery_worker import (
    DeliveryWorker,
    DeliveryWorkerConfig,
)
from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker

from .._support import db_engine, use_real_pg

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not use_real_pg(),
        reason="tick→gate propagation seeds FK rows — requires a reachable Postgres "
        "(BSVIBE_DATABASE_URL); never silently falls back to SQLite",
    ),
]


class _CapturingDispatcher:
    """Records every dispatch. The gate deciding DELIVER routes through here; the
    gate deciding QUEUE never touches it. So ``calls`` is the observable that
    distinguishes held-for-approval from dispatched-directly."""

    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def dispatch(
        self,
        *,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
        artifact_type: str,
        plugins: Iterable[object] = (),
        context: object = None,
        event: object = None,
    ) -> DeliveryResult:
        self.calls.append(deliverable_id)
        return DeliveryResult(
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,  # type: ignore[arg-type]  # "code" ∈ ArtifactType
            actions=[],
        )


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, is_pg):
        assert is_pg, "tick→gate propagation e2e must run on real Postgres"
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_workspace_and_product(
    sf: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    """A workspace with Safe Mode OFF (the whole point — the hold must NOT come
    from the workspace flag) + a product for the tick to target."""
    ws = uuid.uuid4()
    product = uuid.uuid4()
    async with sf() as s:
        s.add(WorkspaceRow(id=ws, name="Tick WS", timezone="UTC", language="en", safe_mode=False))
        await s.flush()
        s.add(
            ProductRow(
                id=product,
                workspace_id=ws,
                name="tick-product",
                slug=uuid.uuid4().hex[:12],
            )
        )
        await s.commit()
    return ws, product


async def _fire_emitter(
    sf: async_sessionmaker[AsyncSession],
    *,
    ws: uuid.UUID,
    product: uuid.UUID,
    kind: str,
    schedule_payload: dict[str, object] | None,
) -> None:
    """Drive the REAL schedule emitter for one fired window — it stamps the
    trigger ``kind`` onto the TriggerEvent payload. No payload["kind"] is set by
    hand anywhere in this test; this is the sole origin of the kind."""
    async with sf() as s:
        await ScheduleTrigger(s).fire(
            workspace_id=ws,
            schedule_id=uuid.uuid4(),
            kind=kind,
            schedule_payload=schedule_payload,
            cron_expr="0 9 * * *",
            product_id=product,
        )
        await s.commit()


async def _open_run_from_intake(
    sf: async_sessionmaker[AsyncSession], *, ws: uuid.UUID
) -> uuid.UUID:
    """IntakeWorker mints the Request (kind rides through Receive), then the REAL
    ``AgentRunner.open_run`` propagates it onto the run payload. Returns run_id."""
    assert await IntakeWorker(session_factory=sf).drain_once() == 1
    async with sf() as s:
        request = (
            await s.execute(select(RequestRow).where(RequestRow.workspace_id == ws))
        ).scalar_one()
        run_id = await AgentRunner(s).open_run(request=request)
        await s.commit()
    return run_id


async def _seed_deliverable_and_event(
    sf: async_sessionmaker[AsyncSession], *, ws: uuid.UUID, run_id: uuid.UUID
) -> uuid.UUID:
    """The DOWNSTREAM artifact (a verified deliverable + its DeliveryEvent). This
    is not the kind seam — the seam is ``run.payload["kind"]``, already set by the
    real chain above. The DeliveryEventRow.run_id points the gate back at that run."""
    async with sf() as s:
        deliverable = Deliverable(
            id=uuid.uuid4(),
            run_id=run_id,
            workspace_id=ws,
            deliverable_type=DeliverableType.CODE,
            payload={"artifact_refs": ["out.txt"], "summary": "Tick result\nbody"},
        )
        s.add(deliverable)
        await s.flush()
        s.add(
            DeliveryEventRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                deliverable_id=deliverable.id,
                run_id=run_id,
                artifact_type=DeliverableType.CODE.value,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
        return deliverable.id


async def test_product_tick_deliverable_is_held_by_safe_mode_gate(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A product_tick tick, with Safe Mode OFF and no binding, is HELD for approval
    — because the ``product_tick`` kind propagated emitter→intake→open_run all the
    way to the gate. Nothing set payload["kind"] by hand."""
    ws, product = await _seed_workspace_and_product(sf)

    # Real emitter → intake → open_run: the kind is stamped once (emitter) and
    # propagated, never hand-set.
    await _fire_emitter(
        sf, ws=ws, product=product, kind=SCHEDULE_KIND_PRODUCT_TICK, schedule_payload=None
    )
    run_id = await _open_run_from_intake(sf, ws=ws)

    # PROOF the propagation reached the run payload (the seam) via the real chain.
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.payload.get("kind") == "product_tick", (
            "the product_tick kind did not propagate emitter→intake→open_run — the "
            "autonomous-origin gate would silently NOT fire"
        )

    deliverable_id = await _seed_deliverable_and_event(sf, ws=ws, run_id=run_id)

    # REAL gate: Safe Mode OFF, no binding → the ONLY reason to hold is the
    # propagated autonomous-origin kind.
    dispatcher = _CapturingDispatcher()
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=dispatcher,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert await worker.drain_once() == 1

    # Held, NOT dispatched: enqueued into the SafeModeQueue, dispatcher untouched.
    assert dispatcher.calls == [], "autonomous product_tick deliverable was dispatched, not held"
    async with sf() as s:
        items = (
            (
                await s.execute(
                    select(SafeModeQueueItemRow).where(SafeModeQueueItemRow.workspace_id == ws)
                )
            )
            .scalars()
            .all()
        )
        assert len(items) == 1, "the tick deliverable was not held for founder approval"
        assert items[0].status is SafeModeStatus.PENDING
        assert items[0].deliverable_id == deliverable_id
        assert items[0].run_id == run_id


async def test_instruction_run_is_dispatched_directly_under_the_same_conditions(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Companion — a NON-product_tick (instruction) run under the SAME Safe-Mode-OFF,
    no-binding conditions is DISPATCHED, not queued. Proves the gate keys on the
    propagated ``product_tick`` kind specifically, not a blanket hold on tick-origin
    runs."""
    ws, product = await _seed_workspace_and_product(sf)

    await _fire_emitter(
        sf,
        ws=ws,
        product=product,
        kind="instruction",
        schedule_payload={"text": "post the weekly summary"},
    )
    run_id = await _open_run_from_intake(sf, ws=ws)

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        # The instruction kind propagated too — it's simply NOT an autonomous-origin
        # kind, so the gate must let it through.
        assert run.payload.get("kind") == "instruction"

    deliverable_id = await _seed_deliverable_and_event(sf, ws=ws, run_id=run_id)

    dispatcher = _CapturingDispatcher()
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=dispatcher,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert await worker.drain_once() == 1

    # Dispatched directly, NOT held.
    assert dispatcher.calls == [deliverable_id], "instruction deliverable was not dispatched"
    async with sf() as s:
        items = (
            (
                await s.execute(
                    select(SafeModeQueueItemRow).where(SafeModeQueueItemRow.workspace_id == ws)
                )
            )
            .scalars()
            .all()
        )
        assert items == [], "an instruction deliverable was wrongly held for approval"
