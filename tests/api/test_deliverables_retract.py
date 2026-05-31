"""POST /api/v1/deliverables/{deliverable_id}/retract — B12b retract endpoint.

Workflow §1.2 + §9 — retraction calls the plugin's ``@p.compensate`` handler
with the captured ``compensation_handle`` so a delivered direct-mode artifact
can be rolled back. Soft guarantees:

* 200 + ``retracted_at`` set on success (idempotent re-call → 200, same state);
* 400 ``no_compensation_handle`` when nothing was captured (plugin opted out
  or row pre-dates B12b);
* 502 when the compensation dispatch raises — the row is NOT marked retracted
  so the operator can see the failure and retry;
* 404 for an unknown / cross-workspace deliverable id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.api.v1.deliverables import get_retract_handler
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionBase,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


class _RecordingRetractor:
    """In-test ``RetractHandler`` — records dispatched compensations."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raises = raises

    async def compensate(
        self,
        *,
        plugin: str,
        artifact_type: str,
        handle: dict[str, Any],
        workspace_id: uuid.UUID,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "plugin": plugin,
                "artifact_type": artifact_type,
                "handle": handle,
                "workspace_id": workspace_id,
            }
        )
        if self._raises is not None:
            raise self._raises
        return {"status": "compensated"}


@pytest_asyncio.fixture
async def sf():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def retractor() -> _RecordingRetractor:
    return _RecordingRetractor()


@pytest_asyncio.fixture
async def client(sf, workspace_id: uuid.UUID, retractor: _RecordingRetractor):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_retract_handler] = lambda: retractor

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_deliverable(
    sf_: async_sessionmaker,
    *,
    workspace_id: uuid.UUID,
    compensation_handles: list[dict[str, Any]] | None = None,
    retracted_at: datetime | None = None,
    deliverable_type: DeliverableType = DeliverableType.PR,
) -> uuid.UUID:
    """Seed a parent run + a Deliverable; return its id."""
    deliverable_id = uuid.uuid4()
    run_id = uuid.uuid4()
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
                payload={"summary": "fix"},
                compensation_handles=compensation_handles,
                retracted_at=retracted_at,
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return deliverable_id


async def test_retract_calls_compensate_and_marks_retracted(
    client: httpx.AsyncClient,
    sf: async_sessionmaker,
    workspace_id: uuid.UUID,
    retractor: _RecordingRetractor,
) -> None:
    handle = {"kind": "pr", "owner": "acme", "repo": "site", "number": 7}
    deliverable_id = await _seed_deliverable(
        sf,
        workspace_id=workspace_id,
        compensation_handles=[{"plugin": "github", "artifact_type": "pr", "handle": handle}],
    )

    resp = await client.post(f"/api/v1/deliverables/{deliverable_id}/retract")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deliverable_id"] == str(deliverable_id)
    assert body["retracted"] is True
    assert isinstance(body["compensated"], list)
    assert len(body["compensated"]) == 1
    entry = body["compensated"][0]
    assert entry["plugin"] == "github"
    assert entry["artifact_type"] == "pr"

    # The recorded compensate call carried the stored handle.
    assert len(retractor.calls) == 1
    call = retractor.calls[0]
    assert call["plugin"] == "github"
    assert call["artifact_type"] == "pr"
    assert call["handle"] == handle
    assert call["workspace_id"] == workspace_id

    # DB state: retracted_at populated.
    async with sf() as s:
        row = await s.get(Deliverable, deliverable_id)
        assert row is not None
        assert row.retracted_at is not None


async def test_retract_without_handle_returns_400(
    client: httpx.AsyncClient,
    sf: async_sessionmaker,
    workspace_id: uuid.UUID,
    retractor: _RecordingRetractor,
) -> None:
    """A deliverable with no captured handle (pre-B12b, or plugin opted out)
    cannot be retracted — 400 ``no_compensation_handle``."""
    deliverable_id = await _seed_deliverable(
        sf, workspace_id=workspace_id, compensation_handles=None
    )

    resp = await client.post(f"/api/v1/deliverables/{deliverable_id}/retract")
    assert resp.status_code == 400, resp.text
    assert "no_compensation_handle" in resp.text
    assert retractor.calls == []
    async with sf() as s:
        row = await s.get(Deliverable, deliverable_id)
        assert row is not None
        assert row.retracted_at is None


async def test_retract_dispatch_failure_returns_502_and_not_retracted(
    client: httpx.AsyncClient,
    sf: async_sessionmaker,
    workspace_id: uuid.UUID,
) -> None:
    """When the compensate dispatch raises, the row is NOT marked retracted —
    so the operator can see the failure (and retry). Use a fresh retractor with
    raises set; override the dependency directly so it overrides the fixture's
    default no-op retractor."""
    handle = {"kind": "pr", "number": 7}
    deliverable_id = await _seed_deliverable(
        sf,
        workspace_id=workspace_id,
        compensation_handles=[{"plugin": "github", "artifact_type": "pr", "handle": handle}],
    )
    failing = _RecordingRetractor(raises=RuntimeError("github 500"))
    client._transport.app.dependency_overrides[get_retract_handler] = lambda: failing  # type: ignore[attr-defined]

    resp = await client.post(f"/api/v1/deliverables/{deliverable_id}/retract")
    assert resp.status_code == 502, resp.text
    assert "github 500" in resp.text or "compensate_failed" in resp.text
    async with sf() as s:
        row = await s.get(Deliverable, deliverable_id)
        assert row is not None
        assert row.retracted_at is None


async def test_retract_is_idempotent(
    client: httpx.AsyncClient,
    sf: async_sessionmaker,
    workspace_id: uuid.UUID,
    retractor: _RecordingRetractor,
) -> None:
    """Re-retracting an already-retracted deliverable is a 200 no-op — no
    second compensate call (handlers are idempotent at the plugin layer, but
    the API short-circuits to avoid even attempting it)."""
    handle = {"kind": "pr", "number": 7}
    already = datetime.now(tz=UTC)
    deliverable_id = await _seed_deliverable(
        sf,
        workspace_id=workspace_id,
        compensation_handles=[{"plugin": "github", "artifact_type": "pr", "handle": handle}],
        retracted_at=already,
    )

    resp = await client.post(f"/api/v1/deliverables/{deliverable_id}/retract")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retracted"] is True
    assert body.get("already_retracted") is True
    assert retractor.calls == []  # short-circuited


async def test_retract_unknown_deliverable_404(
    client: httpx.AsyncClient,
    retractor: _RecordingRetractor,
) -> None:
    resp = await client.post(f"/api/v1/deliverables/{uuid.uuid4()}/retract")
    assert resp.status_code == 404
    assert retractor.calls == []


async def test_retract_cross_workspace_deliverable_404(
    client: httpx.AsyncClient,
    sf: async_sessionmaker,
    workspace_id: uuid.UUID,
    retractor: _RecordingRetractor,
) -> None:
    """A deliverable belonging to a different workspace is invisible — 404."""
    other_ws = uuid.uuid4()
    deliverable_id = await _seed_deliverable(
        sf,
        workspace_id=other_ws,
        compensation_handles=[{"plugin": "github", "artifact_type": "pr", "handle": {"number": 1}}],
    )
    resp = await client.post(f"/api/v1/deliverables/{deliverable_id}/retract")
    assert resp.status_code == 404
    assert retractor.calls == []
