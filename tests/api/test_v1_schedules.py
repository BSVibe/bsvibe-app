"""/api/v1/schedules — schedule authoring CRUD (S1).

Exercises the real ScheduleService + repository + INV-1 producer emit against an
in-memory SQLite (or real PG when ``BSVIBE_DATABASE_URL`` is set). Only the auth
/ session / workspace deps are overridden — the schedule creation itself runs
through the production path.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.schedule.infrastructure.schedule_db import WorkspaceScheduleRow

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(db, workspace_id):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_create_lists_and_returns_view(client, db, workspace_id) -> None:
    resp = await client.post(
        "/api/v1/schedules",
        json={
            "kind": "instruction",
            "text": "post the weekly market summary",
            "cron_expr": "0 9 * * 1",
            "title": "Weekly summary",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "instruction"
    assert body["text"] == "post the weekly market summary"
    assert body["cron_expr"] == "0 9 * * 1"
    assert body["title"] == "Weekly summary"
    assert body["enabled"] is True
    assert body["next_run_at"] is not None
    sched_id = body["id"]

    # The row really landed through the producer emit.
    async with db() as s:
        row = (
            await s.execute(
                select(WorkspaceScheduleRow).where(
                    WorkspaceScheduleRow.workspace_id == workspace_id
                )
            )
        ).scalar_one()
        assert row.payload == {"text": "post the weekly market summary"}
        assert row.plugin_name is None

    listed = await client.get("/api/v1/schedules")
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [sched_id]


async def test_kind_defaults_to_instruction(client) -> None:
    resp = await client.post(
        "/api/v1/schedules",
        json={"text": "do the thing", "cron_expr": "*/5 * * * *"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == "instruction"


async def test_invalid_cron_is_400(client) -> None:
    resp = await client.post(
        "/api/v1/schedules",
        json={"text": "do the thing", "cron_expr": "not a cron"},
    )
    assert resp.status_code == 400, resp.text
    assert "cron" in resp.json()["detail"].lower()


async def test_extra_field_is_rejected(client) -> None:
    resp = await client.post(
        "/api/v1/schedules",
        json={"text": "x", "cron_expr": "* * * * *", "surprise": "nope"},
    )
    assert resp.status_code == 422, resp.text


async def test_empty_text_is_rejected(client) -> None:
    # Empty text is invalid for the instruction kind — the kind rules live in
    # ScheduleService (so product_tick can share the same schema with no text),
    # so the rejection surfaces as a 400, not a schema-level 422.
    resp = await client.post(
        "/api/v1/schedules",
        json={"text": "", "cron_expr": "* * * * *"},
    )
    assert resp.status_code == 400, resp.text
    assert "text" in resp.json()["detail"].lower()


async def test_create_product_tick_requires_product_id_400(client) -> None:
    resp = await client.post(
        "/api/v1/schedules",
        json={"kind": "product_tick", "cron_expr": "0 9 * * *"},
    )
    assert resp.status_code == 400, resp.text
    assert "product" in resp.json()["detail"].lower()


async def test_create_product_tick_with_product_id(client, db, workspace_id) -> None:
    product_id = str(uuid.uuid4())
    resp = await client.post(
        "/api/v1/schedules",
        json={"kind": "product_tick", "cron_expr": "0 9 * * *", "product_id": product_id},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "product_tick"
    assert body["product_id"] == product_id
    assert body["enabled"] is True


async def test_patch_toggles_enabled(client) -> None:
    created = await client.post("/api/v1/schedules", json={"text": "x", "cron_expr": "* * * * *"})
    sched_id = created.json()["id"]

    patched = await client.patch(f"/api/v1/schedules/{sched_id}", json={"enabled": False})
    assert patched.status_code == 200, patched.text
    assert patched.json()["enabled"] is False


async def test_delete_removes_and_404s_thereafter(client) -> None:
    created = await client.post("/api/v1/schedules", json={"text": "x", "cron_expr": "* * * * *"})
    sched_id = created.json()["id"]

    deleted = await client.delete(f"/api/v1/schedules/{sched_id}")
    assert deleted.status_code == 204, deleted.text

    again = await client.delete(f"/api/v1/schedules/{sched_id}")
    assert again.status_code == 404


async def test_patch_missing_schedule_is_404(client) -> None:
    resp = await client.patch(f"/api/v1/schedules/{uuid.uuid4()}", json={"enabled": True})
    assert resp.status_code == 404
