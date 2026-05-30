"""D6 delta 4 — Safe Mode gates EVERY mid-loop partial emission, not just the final.

Synthesis §13 + D3: the per-Resource ``output_mode`` decision is keyed PER-Run
AND per-emission. When a run with ``binding.output_mode == "safe"`` emits
multiple partial Deliverables (each producing its own DeliveryEventRow), the
DeliveryWorker must gate EACH event through :func:`resolve_output_mode_gate` —
all N partials end up on the Safe Mode queue, none dispatch directly. If the
gate were only run once per terminal (or only on the verified-final), a partial
PR / Notion page would silently dispatch even though the founder asked for
manual approval.

Asserted delta: a run with ``output_mode=safe`` + the workspace flag OFF +
THREE mid-loop partial DeliveryEventRows yields THREE SafeModeQueueItemRows,
zero dispatcher calls. Today's gate already evaluates per-row, so this test
pins the contract — a future "gate at the run level" refactor would break it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.delivery.db import (
    DeliveryEventRow,
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from backend.delivery.schema import ActionResult, DeliveryResult
from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.execution.verified_deliverable import PARTIAL_DELIVERABLE_KIND
from backend.workers.delivery_worker import DeliveryWorker, DeliveryWorkerConfig
from backend.workspaces.db import ProductRow, ResourceBindingRow, WorkspaceRow
from tests._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _SinkDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    async def dispatch(self, **kwargs: Any) -> DeliveryResult:
        self.dispatched.append(kwargs)
        return DeliveryResult(
            workspace_id=kwargs["workspace_id"],
            deliverable_id=kwargs["deliverable_id"],
            artifact_type=kwargs["artifact_type"],
            actions=[ActionResult(action="sink", succeeded=True)],
            delivered_at=datetime.now(tz=UTC),
        )


async def test_three_mid_loop_partials_all_gated_to_safe_mode_queue(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Three partial Deliverables on a ``output_mode=safe`` run + workspace flag
    OFF → three SafeModeQueueItemRows, zero direct dispatches. Pins per-emission
    gating: each DeliveryEventRow flows through ``resolve_output_mode_gate``
    independently."""
    ws = uuid.uuid4()
    run_id = uuid.uuid4()
    binding_id = uuid.uuid4()
    product_id = uuid.uuid4()
    account_id = uuid.uuid4()

    async with sf() as s:
        # workspace flag OFF (per-Run gate is the only thing that should queue).
        # FK ordering matters on real PG: workspaces -> {products, connector_accounts}
        # -> resource_bindings/execution_runs. Without explicit ``relationship()``
        # links, SQLAlchemy can't infer the dependency, so we flush parent-first
        # in stages (same pattern as ``test_safe_mode_output_mode_gate.py``).
        s.add(WorkspaceRow(id=ws, name="acme", safe_mode=False))
        await s.flush()
        s.add(ProductRow(id=product_id, workspace_id=ws, name="P", slug="p"))
        s.add(
            ConnectorAccountRow(
                id=account_id,
                workspace_id=ws,
                connector="github",
                webhook_token=f"tok-{uuid.uuid4().hex}",
                signing_secret_ciphertext="cipher",
            )
        )
        await s.flush()
        s.add(
            ResourceBindingRow(
                id=binding_id,
                workspace_id=ws,
                product_id=product_id,
                connector_account_id=account_id,
                resource_id="owner/repo",
                # output_mode=safe → every emission should QUEUE
                output_mode="safe",
            )
        )
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                status=RunStatus.RUNNING,
                payload={"intent_text": "ship release", "binding_id": str(binding_id)},
            )
        )
        await s.flush()
        # Three mid-loop partial Deliverables with their DeliveryEventRows.
        for i, (artifact_type, dt) in enumerate(
            (
                ("pr", DeliverableType.PR),
                ("page", DeliverableType.PAGE),
                ("direct_output", DeliverableType.DIRECT_OUTPUT),
            )
        ):
            deliverable_id = uuid.uuid4()
            s.add(
                Deliverable(
                    id=deliverable_id,
                    run_id=run_id,
                    workspace_id=ws,
                    deliverable_type=dt,
                    payload={
                        "kind": PARTIAL_DELIVERABLE_KIND,
                        "artifact_type": artifact_type,
                        "summary": f"partial #{i}",
                    },
                )
            )
            s.add(
                DeliveryEventRow(
                    id=uuid.uuid4(),
                    workspace_id=ws,
                    run_id=run_id,
                    deliverable_id=deliverable_id,
                    artifact_type=artifact_type,
                    payload={"kind": PARTIAL_DELIVERABLE_KIND, "summary": f"partial #{i}"},
                    created_at=datetime.now(tz=UTC),
                )
            )
        await s.commit()

    sink = _SinkDispatcher()
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=sink,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    processed = await worker.drain_once()

    assert processed == 3, f"expected 3 partial events processed, got {processed}"
    assert sink.dispatched == [], (
        f"expected zero direct dispatches (all gated to Safe Mode), got {sink.dispatched}"
    )
    async with sf() as s:
        items = (await s.execute(select(SafeModeQueueItemRow))).scalars().all()
        assert len(items) == 3, f"expected per-emission gate → 3 queue items, got {len(items)}"
        # All items belong to the same run (per-Run grouping key still threaded).
        assert all(it.run_id == run_id for it in items)
        assert all(it.status is SafeModeStatus.PENDING for it in items)
