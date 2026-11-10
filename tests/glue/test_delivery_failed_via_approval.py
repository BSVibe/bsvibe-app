"""승인 직후 배송이 실패하면 backend 프로세스에서도 ``delivery_failed`` 결정이 생긴다.

``delivery_worker._create_decision`` 은 주입식이다. 워커 런타임만 주입하던 때에는, backend
프로세스에서 도는 Safe Mode 승인(REST / MCP / 텔레그램)이 배송 실패를 만나도 결정이 생기지
않았다(``delivery_failure_decision_unwired``) — 창업자는 알 수 없고 다시 보낼 방법도 없었다.

여기서는 ``create_app()`` 의 lifespan 을 실제로 띄워 연결을 얻고, 실제 승인 처리기 경로를 탄다.
GitHub 플러그인 호출(디스패처)만 실패하는 스텁이다.
"""

from __future__ import annotations

import base64
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.api.v1.safemode import get_delivery_dispatcher
from backend.config import get_settings
from backend.data.rls import workspace_session_scope
from backend.mcp.api import McpPrincipal, ToolContext, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.workflow.application.checkpoint_resolution import (
    CheckpointNotFound,
    resolve_checkpoint,
)
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.domain.delivery import (
    FOUNDER_APPROVED_KEY,
    ActionResult,
    DeliveryResult,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow
from backend.workflow.infrastructure.workers import delivery_worker

from .._support import db_engine, fake_current_user, publishing_workspace

pytestmark = pytest.mark.asyncio

_GITHUB_ERROR = "push rejected: refusing to allow a GitHub App to create or update workflow"


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _unwired_between_tests():
    delivery_worker.set_decision_factory(None)
    yield
    delivery_worker.set_decision_factory(None)


class _GithubFails:
    """The only fake: the GitHub plugin call fails, as in the #1341 run."""

    async def dispatch(self, **kwargs: Any) -> DeliveryResult:
        return DeliveryResult(
            workspace_id=kwargs["workspace_id"],
            deliverable_id=kwargs["deliverable_id"],
            artifact_type=kwargs["artifact_type"],
            actions=[
                ActionResult(action="github:outbound:pr", succeeded=False, error=_GITHUB_ERROR)
            ],
            delivered_at=datetime.now(tz=UTC),
        )


@contextlib.asynccontextmanager
async def started_app(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """``create_app()`` with its lifespan running -- what a backend process does at boot."""
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    app = create_app()
    async with app.router.lifespan_context(app):
        yield app
    get_settings.cache_clear()


async def _seed_queued(sf, workspace_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    run_id, deliverable_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with sf() as s, workspace_session_scope(s, workspace_id):
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                product_id=None,
                status=RunStatus.REVIEW_READY,
                payload={},
                created_at=now,
                updated_at=now,
            )
        )
        await s.flush()
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={"summary": "ship it"},
                created_at=now,
            )
        )
        await s.flush()
        await SafeModeQueue(s).enqueue(
            workspace_id=workspace_id, deliverable_id=deliverable_id, run_id=run_id
        )
        await s.commit()
    return run_id, deliverable_id


async def _assert_failure_decision_and_retry(
    sf, workspace_id: uuid.UUID, run_id: uuid.UUID, deliverable_id: uuid.UUID
) -> None:
    async with sf() as s, workspace_session_scope(s, workspace_id):
        rows = (
            (
                await s.execute(
                    select(Decision).where(
                        Decision.run_id == run_id, Decision.decision == "delivery_failed"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, "승인한 배송이 실패했는데 delivery_failed 결정이 없다"
    decision = rows[0]
    assert decision.status == DecisionStatus.PENDING
    assert decision.payload["deliverable_id"] == str(deliverable_id)
    assert _GITHUB_ERROR in decision.rationale
    assert any(_GITHUB_ERROR in f["error"] for f in decision.payload["failures"])

    before = await _delivery_events(sf, workspace_id, deliverable_id)
    await _resolve(sf, workspace_id, decision.id, "retry")
    after = await _delivery_events(sf, workspace_id, deliverable_id)

    # 승인 경로의 첫 배송이 남긴 이벤트와 구분되도록, 새로 생긴 것만 본다.
    added = [e for e in after if e.id not in {b.id for b in before}]
    assert len(after) == len(before) + 1 and len(added) == 1, (
        "retry(다시 보내기)가 배송 이벤트를 정확히 하나 새로 넣어야 한다"
    )
    # 이벤트 행은 배송 워커가 처리한 뒤에 지워진다 -- 행이 남아 있으면 아직 배송을 기다린다.
    # 창업자의 클릭이 곧 승인이라 Safe Mode가 다시 묻지 않는다.
    assert added[0].payload[FOUNDER_APPROVED_KEY] is True
    assert added[0].run_id == run_id

    # 이미 retry로 처리한 결정을 또 처리해도 이벤트가 또 생기지 않는다(중복 재배송 방지).
    with pytest.raises(CheckpointNotFound):
        await _resolve(sf, workspace_id, decision.id, "retry")
    assert len(await _delivery_events(sf, workspace_id, deliverable_id)) == len(after)


async def _resolve(sf, workspace_id: uuid.UUID, checkpoint_id: uuid.UUID, action_key: str) -> None:
    async with sf() as s, workspace_session_scope(s, workspace_id):
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=checkpoint_id,
            action_key=action_key,
            actor_id=uuid.uuid4(),
        )
        await s.commit()


async def _delivery_events(
    sf, workspace_id: uuid.UUID, deliverable_id: uuid.UUID
) -> list[DeliveryEventRow]:
    async with sf() as s, workspace_session_scope(s, workspace_id):
        return list(
            (
                await s.execute(
                    select(DeliveryEventRow).where(
                        DeliveryEventRow.deliverable_id == deliverable_id
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_create_app_lifespan_wires_the_decision_factory(monkeypatch) -> None:
    from backend.workflow.application.run_persistence import create_decision

    async with started_app(monkeypatch):
        assert delivery_worker._create_decision is create_decision, (
            "backend 시작 시 결정 생성기가 연결되지 않았다 — 승인 경로의 배송 실패가 조용히 묻힌다"
        )


async def test_rest_approve_failure_raises_delivery_failed_decision(monkeypatch, sf) -> None:
    async with started_app(monkeypatch) as app:
        await _rest_approve_fails(app, sf)


async def test_mcp_approve_run_failure_raises_delivery_failed_decision(monkeypatch, sf) -> None:
    async with started_app(monkeypatch):
        await _mcp_approve_run_fails(sf)


async def _rest_approve_fails(app: Any, sf) -> None:
    workspace_id = uuid.uuid4()
    run_id, deliverable_id = await _seed_queued(sf, workspace_id)

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = publishing_workspace(workspace_id)
    app.dependency_overrides[get_current_user_row] = lambda: SimpleNamespace(id=uuid.uuid4())
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_delivery_dispatcher] = lambda: _GithubFails()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(f"/api/v1/safemode/runs/{run_id}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["approved_count"] == 1
    assert r.json()["dispatched_count"] == 0

    await _assert_failure_decision_and_retry(sf, workspace_id, run_id, deliverable_id)


async def _mcp_approve_run_fails(sf) -> None:
    workspace_id = uuid.uuid4()
    run_id, deliverable_id = await _seed_queued(sf, workspace_id)
    registry = ToolRegistry()
    register_all_tools(registry)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        ctx = ToolContext(
            principal=McpPrincipal(
                user_id=uuid.uuid4(),
                workspace_id=workspace_id,
                client_id="dcr-test",
                scopes=frozenset({"mcp:read", "mcp:write"}),
                jti=uuid.uuid4(),
            ),
            session=s,
            extras={"delivery_dispatcher": _GithubFails()},
        )
        out = await registry.call_tool("bsvibe_safe_mode_approve_run", {"run_id": str(run_id)}, ctx)
    assert out["approved_count"] == 1
    assert out["dispatched_count"] == 0

    await _assert_failure_decision_and_retry(sf, workspace_id, run_id, deliverable_id)
