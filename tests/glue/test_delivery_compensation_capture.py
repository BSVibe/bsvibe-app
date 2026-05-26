"""B12b — compensation_handle capture on successful direct-mode delivery.

Workflow §1.2 + §3.1 + §9. A plugin's ``@p.outbound`` returns a
``compensation_handle`` (plugin-private revert token) alongside the
delivered artifact ref. Before B12b that handle was logged + thrown away;
the matching ``@p.compensate`` handler existed but had no path back to
the handle, so a delivered direct-mode artifact could never be rolled
back.

This test pins the capture: when :func:`dispatch_delivery` returns a
``DeliveryResult`` whose actions carry ``output["compensation_handle"]``,
the :class:`Deliverable` row is updated so the retract endpoint can find
the handle later. One row per successful action (per-plugin / per-
artifact_type) — failed actions are NOT captured (nothing to undo).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.delivery.schema import ActionResult, DeliveryResult
from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionBase,
    ExecutionRun,
    RunStatus,
)
from backend.workers.delivery_worker import dispatch_delivery

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _RecordingDispatcher:
    """A :class:`PluginDispatchAdapter` returning a canned DeliveryResult."""

    def __init__(self, result: DeliveryResult) -> None:
        self._result = result

    async def dispatch(self, **_kwargs: Any) -> DeliveryResult:
        return self._result


async def _seed_deliverable(
    sf_: async_sessionmaker, *, deliverable_type: DeliverableType = DeliverableType.PR
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return ``(workspace_id, run_id, deliverable_id)``."""
    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with sf_() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.SHIPPED,
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=deliverable_type,
                payload={"summary": "fix bug"},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return workspace_id, run_id, deliverable_id


async def test_capture_persists_compensation_handle_on_success(sf) -> None:
    """A successful action with a ``compensation_handle`` in its output is
    persisted on the Deliverable so the retract endpoint can later read it."""
    workspace_id, _, deliverable_id = await _seed_deliverable(sf)
    handle = {"kind": "pr", "owner": "acme", "repo": "site", "number": 7}
    result = DeliveryResult(
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        actions=[
            ActionResult(
                action="github:outbound:pr",
                succeeded=True,
                output={
                    "artifact_type": "pr",
                    "external_ref": "github://acme/site/pull/7",
                    "url": "https://github.com/acme/site/pull/7",
                    "compensation_handle": handle,
                },
            )
        ],
        delivered_at=datetime.now(tz=UTC),
    )
    dispatcher = _RecordingDispatcher(result)

    await dispatch_delivery(
        dispatcher,
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        session_factory=sf,
    )

    async with sf() as s:
        row = await s.get(Deliverable, deliverable_id)
        assert row is not None
        assert row.compensation_handles, "expected compensation_handles populated"
        captured = list(row.compensation_handles)
        assert len(captured) == 1
        entry = captured[0]
        assert entry["plugin"] == "github"
        assert entry["artifact_type"] == "pr"
        assert entry["handle"] == handle
        # Untouched: the row has not been retracted yet.
        assert row.retracted_at is None


async def test_capture_skips_failed_actions(sf) -> None:
    """A failed action (no successful outbound) has nothing to compensate."""
    workspace_id, _, deliverable_id = await _seed_deliverable(sf)
    result = DeliveryResult(
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        actions=[
            ActionResult(
                action="github:outbound:pr",
                succeeded=False,
                error="rate limited",
            )
        ],
        delivered_at=datetime.now(tz=UTC),
        error="rate limited",
    )
    await dispatch_delivery(
        _RecordingDispatcher(result),
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        session_factory=sf,
    )
    async with sf() as s:
        row = await s.get(Deliverable, deliverable_id)
        assert row is not None
        # Nothing captured — column stays None (or empty list, by impl).
        assert not row.compensation_handles


async def test_capture_skips_actions_without_handle(sf) -> None:
    """A successful action that did NOT return a handle (plugin opted out) is
    skipped — no fake entry is invented."""
    workspace_id, _, deliverable_id = await _seed_deliverable(sf)
    result = DeliveryResult(
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="page",
        actions=[
            ActionResult(
                action="notion:outbound:page",
                succeeded=True,
                output={"external_ref": "notion://x", "url": "https://notion/x"},
            )
        ],
        delivered_at=datetime.now(tz=UTC),
    )
    await dispatch_delivery(
        _RecordingDispatcher(result),
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="page",
        session_factory=sf,
    )
    async with sf() as s:
        row = await s.get(Deliverable, deliverable_id)
        assert row is not None
        assert not row.compensation_handles


async def test_capture_records_multiple_handles_for_multi_action_dispatch(sf) -> None:
    """Two successful actions (e.g. github PR + linear issue) → two entries."""
    workspace_id, _, deliverable_id = await _seed_deliverable(sf)
    h1 = {"kind": "pr", "number": 7}
    h2 = {"kind": "issue", "id": "LIN-12"}
    result = DeliveryResult(
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        actions=[
            ActionResult(
                action="github:outbound:pr",
                succeeded=True,
                output={"compensation_handle": h1},
            ),
            ActionResult(
                action="linear:outbound:issue",
                succeeded=True,
                output={"compensation_handle": h2},
            ),
        ],
        delivered_at=datetime.now(tz=UTC),
    )
    await dispatch_delivery(
        _RecordingDispatcher(result),
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        session_factory=sf,
    )
    async with sf() as s:
        row = await s.get(Deliverable, deliverable_id)
        assert row is not None
        captured = list(row.compensation_handles or [])
        assert len(captured) == 2
        plugins = {e["plugin"] for e in captured}
        assert plugins == {"github", "linear"}
        handles_by_plugin = {e["plugin"]: e["handle"] for e in captured}
        assert handles_by_plugin["github"] == h1
        assert handles_by_plugin["linear"] == h2
