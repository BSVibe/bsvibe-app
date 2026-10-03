"""#1108 — the next step of a split request keeps the first step's delivery gate.

``AgentRunner.open_run`` copies the Request's ``binding_id`` and ``kind`` onto the
run payload, and the DeliveryWorker reads exactly those two keys to gate the
run's deliverable: the binding's ``output_mode`` (``safe``) and the forced Safe
Mode for an autonomous ``product_tick`` run. ``_maybe_spawn_next_step`` built the
next run's payload without either, so from the SECOND step on:

* a binding set to ``output_mode=safe`` no longer applied — with the workspace
  flag off, step 2 delivered straight out;
* an autonomous tick chain's step 2 escaped the forced Safe Mode.

These tests go all the way to the DeliveryWorker's own readers — the gate is what
must survive, not merely the keys.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import (
    ProductRow,
    ResourceBindingRow,
    WorkspaceRow,
)
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.workers.delivery_worker import (
    _run_autonomous_origin,
    _run_output_mode,
)
from tests._support import memory_session

_STEPS = [
    {"stage": "design", "intent": "설계"},
    {"stage": "implement", "intent": "구현"},
]


async def _seed_first_step(s: Any, *, kind: str | None) -> tuple[ExecutionRun, uuid.UUID]:
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    account_id = uuid.uuid4()
    binding_id = uuid.uuid4()
    s.add(WorkspaceRow(id=workspace_id, name="acme", safe_mode=False))
    await s.flush()
    s.add(ProductRow(id=product_id, workspace_id=workspace_id, name="P", slug="p"))
    s.add(
        ConnectorAccountRow(
            id=account_id,
            workspace_id=workspace_id,
            connector="github",
            webhook_token=f"tok-{uuid.uuid4().hex}",
            signing_secret_ciphertext="cipher",
        )
    )
    await s.flush()
    s.add(
        ResourceBindingRow(
            id=binding_id,
            workspace_id=workspace_id,
            product_id=product_id,
            connector_account_id=account_id,
            resource_id="owner/repo",
            output_mode="safe",
        )
    )
    payload: dict[str, Any] = {
        "intent_text": "결제를 만들어줘",
        "binding_id": str(binding_id),
        "frame": {"artifact_type_hint": "code", "steps": _STEPS},
        "stage": "design",
        "step_index": 0,
        "step_intent": "설계",
    }
    if kind is not None:
        payload["kind"] = kind
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=None,  # non-product → no auto-ship, still chains
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload=payload,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    s.add(run)
    await s.flush()
    return run, binding_id


async def _next_step(s: Any, first: ExecutionRun) -> ExecutionRun:
    await AgentRunner(s).transition(run_id=first.id, to_status=RunStatus.REVIEW_READY)
    rows = (await s.execute(select(ExecutionRun).where(ExecutionRun.id != first.id))).scalars()
    spawned = list(rows)
    assert len(spawned) == 1
    return spawned[0]


async def test_the_bindings_safe_output_mode_reaches_the_second_step() -> None:
    async with memory_session() as s:
        first, _ = await _seed_first_step(s, kind=None)
        assert await _run_output_mode(s, first.id) == "safe"  # control: step 1 is gated

        nxt = await _next_step(s, first)

        assert await _run_output_mode(s, nxt.id) == "safe"


async def test_an_autonomous_chain_stays_forced_into_safe_mode() -> None:
    async with memory_session() as s:
        first, _ = await _seed_first_step(s, kind="product_tick")
        assert await _run_autonomous_origin(s, first.id)  # control: step 1 is forced

        nxt = await _next_step(s, first)

        assert await _run_autonomous_origin(s, nxt.id)


async def test_a_founder_direct_chain_gains_no_kind() -> None:
    """Control — a chain that carried no ``kind`` must not acquire one."""
    async with memory_session() as s:
        first, _ = await _seed_first_step(s, kind=None)

        nxt = await _next_step(s, first)

        assert "kind" not in nxt.payload
        assert not await _run_autonomous_origin(s, nxt.id)
