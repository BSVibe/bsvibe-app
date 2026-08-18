"""한 번의 탭은 그 런 전체를 결정한다 — Safe Mode 는 런 단위 트랜잭션이다.

``delivery_worker`` 가 큐에 넣으며 적어둔 계약:

    ``run_id`` (B12a) threads the originating run onto the queue item so the
    founder can approve **all of a run's accumulated partial Deliver events as
    ONE transaction** (Workflow §1.2).

그런데 폰의 승인/거절 버튼은 알림이 실어 온 ``deliverable_id`` **하나만** 해소했다.
멀티아티팩트 런은 partial 을 N개 내므로 큐에 N행이 생기고 탭 한 번은 1행만 닫는다
→ **N-1 행이 영원히 대기**한다.

prod 실측(2026-08-18): 런 `e3c08708` 하나가 18행을 만들고 **17행이 남았다**.
전체 대기 46건 중 **35건이 이 모양**이었다.

단위는 항목이 아니라 **런**이다.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from backend.connectors.approval_callback import (
    ApprovalConnectorAdapter,
    handle_approval_callback,
)
from backend.connectors.db import ConnectorAccountRow
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.domain.delivery import DeliveryResult
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus
from tests._support import memory_session

pytestmark = pytest.mark.asyncio


class _FakeCipher:
    def decrypt(self, token: str) -> str:  # noqa: ARG002
        return "secret"


class _FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def dispatch(
        self,
        *,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
        artifact_type: str,
        plugins: Any = (),
        context: Any = None,
        event: Any = None,
    ) -> DeliveryResult:
        self.calls.append({"deliverable_id": deliverable_id})
        return DeliveryResult(
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,  # type: ignore[arg-type]
            actions=[],
        )


class _FakeRunner:
    """Duck-typed PluginRunner: parse returns the pre-parsed body; ack / update
    just record the dispatched action name + kwargs (no plugin needed)."""

    def __init__(self, parsed: dict[str, Any]) -> None:
        self._parsed = parsed
        self.actions: list[tuple[str, dict[str, Any]]] = []

    async def dispatch_action(
        self, plugin: Any, *, action_name: str, context: Any, kwargs: dict[str, Any]
    ) -> Any:
        del plugin, context
        if action_name == "parse":
            return self._parsed
        self.actions.append((action_name, kwargs))
        return {"ok": True}


def _adapter(*, authorized: bool = True) -> ApprovalConnectorAdapter:
    return ApprovalConnectorAdapter(
        connector="fake",
        credential_key="fake_token",
        parse_action="parse",
        ack_action="ack",
        update_action="update",
        is_interaction=lambda body: body.get("kind") == "tap",
        is_authorized=lambda parsed, account: authorized,  # noqa: ARG005
        build_ack=lambda parsed, text: {"text": text},
        build_update=lambda parsed, status: {"status": status},
    )


def _account(ws: uuid.UUID) -> ConnectorAccountRow:
    return ConnectorAccountRow(
        workspace_id=ws,
        connector="fake",
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="ciphertext",
        delivery_config={},
        is_active=True,
    )


def _raw(*, verb: str, deliverable_id: str) -> bytes:
    return json.dumps({"kind": "tap", "verb": verb, "deliverable_id": deliverable_id}).encode()


def _parsed(*, verb: str, deliverable_id: str) -> dict[str, Any]:
    return {"verb": verb, "deliverable_id": deliverable_id, "malformed": False}


async def _seed_ws(session, ws: uuid.UUID) -> None:
    owner = UserRow(id=uuid.uuid4(), supabase_user_id=f"sub-{uuid.uuid4().hex}")
    session.add(WorkspaceRow(id=ws, name="WS", language="en"))
    session.add(owner)
    session.add(MembershipRow(user_id=owner.id, workspace_id=ws, role="owner"))


async def _seed_run(session, *, ws: uuid.UUID, partials: int) -> tuple[uuid.UUID, uuid.UUID]:
    """One run that emitted ``partials`` artifacts → that many queue rows.
    Returns (run_id, the deliverable the notification names = the LAST one)."""
    run_id = uuid.uuid4()
    q = SafeModeQueue(session)
    last = uuid.uuid4()
    for _ in range(partials):
        last = uuid.uuid4()
        await q.enqueue(workspace_id=ws, deliverable_id=last, run_id=run_id)
    await session.commit()
    return run_id, last


async def _pending(session, run_id: uuid.UUID) -> int:
    session.expire_all()
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(SafeModeQueueItemRow)
                .where(
                    SafeModeQueueItemRow.run_id == run_id,
                    SafeModeQueueItemRow.status == SafeModeStatus.PENDING,
                )
            )
        ).scalar_one()
    )


async def _tap(session, *, ws: uuid.UUID, verb: str, deliverable_id: uuid.UUID, dispatcher):
    runner = _FakeRunner(_parsed(verb=verb, deliverable_id=str(deliverable_id)))
    return await handle_approval_callback(
        adapter=_adapter(),
        raw_body=_raw(verb=verb, deliverable_id=str(deliverable_id)),
        account=_account(ws),
        session=session,
        plugin=object(),
        cipher=_FakeCipher(),
        dispatcher=dispatcher,
        runner=runner,  # type: ignore[arg-type]
    )


async def test_one_approve_tap_settles_the_whole_run() -> None:
    """탭 한 번 = 런 하나. 대기 행이 남으면 안 된다."""
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        await _seed_ws(session, ws)
        run_id, tapped = await _seed_run(session, ws=ws, partials=4)
        assert await _pending(session, run_id) == 4

        await _tap(session, ws=ws, verb="apv", deliverable_id=tapped, dispatcher=dispatcher)

        assert await _pending(session, run_id) == 0, (
            "런의 partial 이 대기로 남았다 — 탭이 항목 하나만 닫았다"
        )
    # 행만 뒤집고 안 보내면 소용없다 — 승인은 발송까지 간다.
    assert len(dispatcher.calls) == 4


async def test_one_reject_tap_settles_the_whole_run() -> None:
    """거절도 대칭이어야 한다 — REST 에는 런 단위 거절 자체가 없었다."""
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        await _seed_ws(session, ws)
        run_id, tapped = await _seed_run(session, ws=ws, partials=3)

        await _tap(session, ws=ws, verb="rej", deliverable_id=tapped, dispatcher=dispatcher)

        assert await _pending(session, run_id) == 0, "거절이 항목 하나만 닫았다"
    assert dispatcher.calls == [], "거절은 절대 발송하지 않는다"


async def test_an_item_without_a_run_still_settles_alone() -> None:
    """``run_id`` 는 nullable 이다(구 행·직접 발행). 그 경우 단일 항목 동작 유지."""
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        await _seed_ws(session, ws)
        d = uuid.uuid4()
        item_id = await SafeModeQueue(session).enqueue(workspace_id=ws, deliverable_id=d)
        await session.commit()

        await _tap(session, ws=ws, verb="apv", deliverable_id=d, dispatcher=dispatcher)

        session.expire_all()
        row = await session.get(SafeModeQueueItemRow, item_id)
        assert row is not None
        assert row.status is SafeModeStatus.APPROVED


async def test_another_runs_items_are_untouched() -> None:
    """런 단위여도 남의 런까지 휩쓸면 안 된다."""
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        await _seed_ws(session, ws)
        run_a, tapped_a = await _seed_run(session, ws=ws, partials=2)
        run_b, _ = await _seed_run(session, ws=ws, partials=3)

        await _tap(session, ws=ws, verb="apv", deliverable_id=tapped_a, dispatcher=dispatcher)

        assert await _pending(session, run_a) == 0
        assert await _pending(session, run_b) == 3, "다른 런이 휩쓸렸다"
