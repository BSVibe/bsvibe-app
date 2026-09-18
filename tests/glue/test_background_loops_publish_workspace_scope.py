"""배경 루프가 처리 중인 행의 워크스페이스를 publish 하는가 (#959).

#959 의 판정 문장 그대로다:

> 잊힌 필터는 REST 에서 라우트 레벨로 잡히고, MCP 는 디스패처에서 구조적으로
> 잡히고, **배경 코드에서는 아무것도 안 잡는다.**

요청 경로는 두 겹으로 지켜진다 — 레이어 2(ORM 자동필터)와 레이어 3(RLS GUC)은
둘 다 `current_workspace_id` contextvar 에서 나온다. 배경 워커는 그 contextvar 를
세우지 않으므로 **둘이 동시에 no-op** 이고, 쿼리는 DB 에서 무필터로 돈다.

`agent_worker.drive_once` 는 이미 고쳐져 있다(클레임이 `(run_id, workspace_id)` 를
돌려주는 지점에서 `workspace_scope`). 나머지 배경 루프는 아직이다.

⚠️ 이 테스트가 **워크스페이스를 둘** 쓰는 이유: 하나만 쓰면
`workspace_scope(아무거나)` 같은 구현도 통과한다. 행마다 **그 행의** 워크스페이스가
publish 되는지는 서로 다른 두 행을 처리시켜야만 드러난다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data.scoping import current_workspace_id
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow
from backend.workflow.infrastructure.workers.delivery_worker import (
    DeliveryWorker,
    DeliveryWorkerConfig,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _ScopeRecordingDispatcher:
    """Records the AMBIENT workspace at dispatch time, not the argument.

    The row's ``workspace_id`` is passed in as a kwarg either way — reading that
    back would prove nothing. What matters is whether the contextvar that
    layers 2 and 3 read is published while the row is being handled.
    """

    def __init__(self) -> None:
        self.ambient: list[uuid.UUID | None] = []

    async def dispatch(self, **kwargs: Any) -> DeliveryResult:
        self.ambient.append(current_workspace_id.get())
        return DeliveryResult(
            workspace_id=kwargs["workspace_id"],
            deliverable_id=kwargs["deliverable_id"],
            artifact_type=kwargs["artifact_type"],
            actions=[ActionResult(action="sink", succeeded=True)],
            delivered_at=datetime.now(tz=UTC),
        )


async def _seed_event(sf_: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID) -> None:
    """A workspace with safe-mode OFF + a run + one pending delivery event."""
    run_id = uuid.uuid4()
    async with sf_() as s:
        s.add(WorkspaceRow(id=workspace_id, name=f"ws-{workspace_id.hex[:6]}", safe_mode=False))
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.SHIPPED,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            DeliveryEventRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                run_id=run_id,
                deliverable_id=uuid.uuid4(),
                artifact_type="code",
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()


async def test_delivery_worker_publishes_each_rows_workspace(sf) -> None:
    """Every dispatched row must run under ITS OWN workspace scope."""
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    await _seed_event(sf, ws_a)
    await _seed_event(sf, ws_b)

    sink = _ScopeRecordingDispatcher()
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=sink,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    await worker.drain_once()

    # 양성 대조군 — 아무것도 디스패치되지 않았다면 아래 단언이 공허하게 통과한다.
    assert len(sink.ambient) == 2, f"두 행이 디스패치되지 않았다: {sink.ambient}"
    assert None not in sink.ambient, (
        "배경 루프가 워크스페이스를 publish 하지 않았다 — 레이어 2(ORM 자동필터)와 "
        f"레이어 3(RLS GUC)이 동시에 no-op 이다. ambient={sink.ambient}"
    )
    assert set(sink.ambient) == {ws_a, ws_b}, (
        "publish 된 워크스페이스가 행의 워크스페이스와 다르다 — 한 값으로 고정돼 "
        f"있으면 이 단언이 잡는다. ambient={sink.ambient}"
    )


async def test_the_scope_is_released_after_the_batch(sf) -> None:
    """스코프가 새면 다음 클레임이 테넌트-블라인드가 아니게 된다.

    `execution_runs` 는 RLS-FORCED 라, 클레임 쿼리에 스코프가 남아 붙으면
    그 쿼리가 fail-closed 가 되어 **다른 워크스페이스의 파이프라인이 멈춘다**.
    """
    await _seed_event(sf, uuid.uuid4())
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=_ScopeRecordingDispatcher(),
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    await worker.drain_once()

    assert current_workspace_id.get() is None, (
        "배치가 끝난 뒤에도 워크스페이스가 남아 있다 — 다음 클레임이 "
        "테넌트-블라인드가 아니게 되어 파이프라인이 멈출 수 있다"
    )


# ── notify_worker ────────────────────────────────────────────────────────────
#
# A SECOND call site needs a SECOND test. A guard on the shared helper
# (``workspace_scope`` itself) proves nothing about whether a given loop
# actually wired it — that is how the delivery loop stayed unguarded while the
# agent loop was already fixed.


class _ScopeRecordingSender:
    """Records the AMBIENT workspace at send time."""

    def __init__(self) -> None:
        self.ambient: list[uuid.UUID | None] = []

    async def send(self, **kwargs: Any) -> None:
        del kwargs
        self.ambient.append(current_workspace_id.get())


async def _seed_notification(sf_: async_sessionmaker[AsyncSession], ws: uuid.UUID) -> None:
    """A workspace with a bound telegram channel + one pending outbox row."""
    from backend.connectors.db import ConnectorAccountRow
    from backend.notifications.db import NotificationEventRow, NotificationPrefsRow

    async with sf_() as s:
        s.add(WorkspaceRow(id=ws, name=f"ws-{ws.hex[:6]}", timezone="UTC", language="en"))
        s.add(
            ConnectorAccountRow(
                workspace_id=ws,
                connector="telegram",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext="ciphertext",
                delivery_config={"chat_id": "42"},
                is_active=True,
            )
        )
        s.add(NotificationPrefsRow(workspace_id=ws, matrix={"needs_you": ["telegram"]}))
        s.add(
            NotificationEventRow(
                workspace_id=ws,
                event="needs_you",
                dedupe_key=f"needs_you:{ws}",
                payload={"question": "q"},
            )
        )
        await s.commit()


async def test_notify_worker_publishes_each_rows_workspace(sf) -> None:
    from backend.workflow.infrastructure.workers.notify_worker import NotifyWorker

    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    await _seed_notification(sf, ws_a)
    await _seed_notification(sf, ws_b)

    sender = _ScopeRecordingSender()
    worker = NotifyWorker(session_factory=sf, sender=sender, pwa_url="https://app.example")
    await worker.drain_once()

    assert len(sender.ambient) == 2, f"두 행이 전송되지 않았다: {sender.ambient}"
    assert None not in sender.ambient, (
        f"notify 루프가 워크스페이스를 publish 하지 않았다. ambient={sender.ambient}"
    )
    assert set(sender.ambient) == {ws_a, ws_b}, (
        f"행의 워크스페이스와 다르다. ambient={sender.ambient}"
    )
