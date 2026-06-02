"""REST surface for ``/api/v1/inside/trust/*`` — fleet + per-product.

Lift M4a end-to-end on the HTTP boundary. Each test drives the production
FastAPI app with overrides for ``get_workspace_id`` + ``get_db_session``
(so the test session points at a tmp SQLite stand-in). The
:class:`TrustSurfaceService` runs against real seeded rows — no mocks at
the service boundary; the test exercises the real query paths.

Asserts:

* ``GET /trust/fleet`` returns one entry per distinct product in the
  workspace, each with a glyph + reason.
* Empty workspace ``GET /trust/fleet`` returns ``{"products": []}``.
* ``GET /trust/{product_id}`` returns the composed metric shape
  (touch_time + deposit_rate + trend_arrow + contract_strength).
* Unknown product_id still returns a valid (dormant) shape — never 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.workers.db import SettleDrainRow
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_run(
    workspace_id: uuid.UUID,
    product_id: uuid.UUID,
    *,
    status: RunStatus = RunStatus.SHIPPED,
    created_at: datetime = _NOW,
) -> ExecutionRun:
    return ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=product_id,
        status=status,
        payload={},
        created_at=created_at,
        updated_at=created_at,
    )


async def test_fleet_empty_workspace(client: httpx.AsyncClient):
    """Empty workspace → ``products: []`` (never an error)."""
    r = await client.get("/api/v1/inside/trust/fleet")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"products": []}


async def test_fleet_lists_one_entry_per_product(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
):
    """One entry per distinct product_id with a glyph + reason."""
    p1 = uuid.uuid4()
    p2 = uuid.uuid4()
    p3 = uuid.uuid4()
    async with sf() as s:
        s.add_all(
            [
                _make_run(workspace_id, p1, created_at=_NOW - timedelta(days=1)),
                _make_run(workspace_id, p2, created_at=_NOW - timedelta(days=1)),
                _make_run(workspace_id, p3, created_at=_NOW - timedelta(days=1)),
            ]
        )
        await s.commit()
    r = await client.get("/api/v1/inside/trust/fleet")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["products"]) == 3
    product_ids = {entry["product_id"] for entry in body["products"]}
    assert product_ids == {str(p1), str(p2), str(p3)}
    for entry in body["products"]:
        assert entry["trend_arrow"]["glyph"] in {"↗", "→", "↘", "·"}
        assert isinstance(entry["trend_arrow"]["reason"], str)


async def test_product_trust_detail_shape(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
):
    """``GET /trust/{product_id}`` returns all four sub-metrics."""
    product_id = uuid.uuid4()
    actor = uuid.uuid4()
    run = _make_run(workspace_id, product_id, created_at=_NOW - timedelta(days=20))
    decision = Decision(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=workspace_id,
        decision="ask_user_question",
        status=DecisionStatus.RESOLVED,
        created_at=_NOW - timedelta(hours=2),
        resolved_at=_NOW - timedelta(hours=1),
        resolved_by=actor,
        payload={},
    )
    drain = SettleDrainRow(
        activity_id=uuid.uuid4(),
        workspace_id=workspace_id,
        run_id=run.id,
        node_ref="garden/seedling/x.md",
        drained_at=_NOW - timedelta(days=2),
    )
    async with sf() as s:
        s.add_all([run, decision, drain])
        await s.commit()

    r = await client.get(f"/api/v1/inside/trust/{product_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["product_id"] == str(product_id)
    assert body["touch_time"]["decisions_resolved_count"] == 1
    assert body["touch_time"]["window_days"] == 14
    assert body["deposit_rate"]["deposit_count"] == 1
    assert body["trend_arrow"]["glyph"] in {"↗", "→", "↘", "·"}
    assert "is_steady" in body["contract_strength"]


async def test_product_trust_dormant_for_unknown_product(
    client: httpx.AsyncClient,
    workspace_id: uuid.UUID,
):
    """An unknown product_id still returns a valid (dormant) shape — never 404.

    Per design §3.4, dormant products carry the ``·`` glyph; the surface
    is a constant shape so the PWA never has to special-case a missing
    response.
    """
    unknown = uuid.uuid4()
    r = await client.get(f"/api/v1/inside/trust/{unknown}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["product_id"] == str(unknown)
    assert body["trend_arrow"]["glyph"] == "·"
    assert body["touch_time"]["decisions_resolved_count"] == 0
    assert body["deposit_rate"]["deposit_count"] == 0
