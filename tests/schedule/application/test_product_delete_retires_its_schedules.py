"""#961 — deleting a product retires the schedules that name it.

Product delete cancelled the product's runs and removed its repo, but left its
``workspace_schedules`` rows alone (``product_id`` is a loose reference, no FK). A
``product_tick`` row then kept firing every cadence for a product that no longer existed —
"decide the next action for <nothing>". Making ticks authorable from the PWA (#961) makes
that easy to reach, so it is closed first:

* a ``product_tick`` schedule IS the product's cadence — without it, it means nothing →
  deleted;
* an ``instruction`` schedule pinned to the product carries the founder's own words → kept,
  but switched OFF, so nothing is lost and nothing runs against a missing product;
* schedules of other products, and of other workspaces, are untouched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from backend.schedule.application.schedule_service import ScheduleService
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


async def _make(service: ScheduleService, ws: uuid.UUID, kind: str, product: uuid.UUID | None):
    return await service.create(
        workspace_id=ws,
        kind=kind,
        text="" if kind == "product_tick" else "weekly report",
        cron_expr="0 9 * * 1",
        product_id=product,
        now=_NOW,
    )


async def test_deleting_a_product_retires_exactly_its_schedules() -> None:
    async with memory_session() as session:
        service = ScheduleService(session)
        ws, gone, kept = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        tick = await _make(service, ws, "product_tick", gone)
        pinned = await _make(service, ws, "instruction", gone)
        other_tick = await _make(service, ws, "product_tick", kept)
        unpinned = await _make(service, ws, "instruction", None)
        other_ws_tick = await _make(service, uuid.uuid4(), "product_tick", gone)
        await session.commit()

        deleted, disabled = await service.retire_for_product(workspace_id=ws, product_id=gone)
        await session.commit()

        assert (deleted, disabled) == (1, 1)
        assert await service.get(schedule_id=tick.id, workspace_id=ws) is None
        still = await service.get(schedule_id=pinned.id, workspace_id=ws)
        assert still is not None and still.enabled is False
        assert still.payload["text"] == "weekly report", "the founder's words survive"
        for row in (other_tick, unpinned):
            assert (await service.get(schedule_id=row.id, workspace_id=ws)).enabled is True  # type: ignore[union-attr]
        assert (
            await service.get(schedule_id=other_ws_tick.id, workspace_id=other_ws_tick.workspace_id)
        ) is not None


def test_the_delete_endpoint_retires_the_products_schedules() -> None:
    """The endpoint is the only product-delete path (MCP has none); unmeasured, the wiring
    breaks silently and the tick goes back to firing for a deleted product."""
    import inspect

    from backend.api.v1.products import products_crud

    # Whitespace-insensitive: the formatter is free to wrap the call.
    src = "".join(inspect.getsource(products_crud.delete_product).split())
    call = "retire_for_product(workspace_id=workspace_id,product_id=product_id)"
    assert call in src
    assert src.index(call) < src.index("awaitsession.commit()")
