"""Connector-agnostic interactive-approval handler — the shared flow, exercised
through a FAKE connector adapter (no telegram specifics).

The telegram adapter's own end-to-end behaviour is pinned in
``test_telegram_callback.py``; here we prove the neutral core
(:func:`handle_approval_callback`) with a minimal fake adapter + a fake plugin
runner — showing the seam a future Slack / Discord adapter plugs into: an
``is_interaction`` predicate, an ``is_authorized`` decision, ``build_ack`` /
``build_update`` kwargs-builders, and three ``@p.action`` names.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

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


async def _seed(session, *, ws: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    owner = UserRow(id=uuid.uuid4(), supabase_user_id=f"sub-{uuid.uuid4().hex}")
    session.add(WorkspaceRow(id=ws, name="WS", language="en"))
    session.add(owner)
    session.add(MembershipRow(user_id=owner.id, workspace_id=ws, role="owner"))
    deliverable_id = uuid.uuid4()
    item_id = await SafeModeQueue(session).enqueue(workspace_id=ws, deliverable_id=deliverable_id)
    await session.commit()
    return item_id, deliverable_id


async def _status(session, item_id: uuid.UUID) -> SafeModeStatus:
    session.expire_all()
    row = await session.get(SafeModeQueueItemRow, item_id)
    assert row is not None
    return row.status


def _raw(*, verb: str, deliverable_id: str) -> bytes:
    return json.dumps({"kind": "tap", "verb": verb, "deliverable_id": deliverable_id}).encode()


def _parsed(*, verb: str, deliverable_id: str) -> dict[str, Any]:
    return {"verb": verb, "deliverable_id": deliverable_id, "malformed": False}


async def test_non_interaction_body_returns_false_for_fall_through() -> None:
    adapter = _adapter()
    runner = _FakeRunner({})
    async with memory_session() as session:
        handled = await handle_approval_callback(
            adapter=adapter,
            raw_body=json.dumps({"kind": "other"}).encode(),
            account=_account(uuid.uuid4()),
            session=session,
            plugin=object(),
            cipher=_FakeCipher(),
            runner=runner,  # type: ignore[arg-type]
        )
    assert handled is False
    assert runner.actions == []


async def test_authorized_approve_dispatches_acks_and_updates() -> None:
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        runner = _FakeRunner(_parsed(verb="apv", deliverable_id=str(deliverable_id)))
        handled = await handle_approval_callback(
            adapter=_adapter(),
            raw_body=_raw(verb="apv", deliverable_id=str(deliverable_id)),
            account=_account(ws),
            session=session,
            plugin=object(),
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
            runner=runner,  # type: ignore[arg-type]
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.APPROVED
    assert len(dispatcher.calls) == 1
    names = [a[0] for a in runner.actions]
    assert names == ["ack", "update"]  # ack THEN card update


async def test_unauthorized_tap_acks_and_leaves_item_pending() -> None:
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        runner = _FakeRunner(_parsed(verb="apv", deliverable_id=str(deliverable_id)))
        handled = await handle_approval_callback(
            adapter=_adapter(authorized=False),
            raw_body=_raw(verb="apv", deliverable_id=str(deliverable_id)),
            account=_account(ws),
            session=session,
            plugin=object(),
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
            runner=runner,  # type: ignore[arg-type]
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.PENDING
    assert dispatcher.calls == []
    assert [a[0] for a in runner.actions] == ["ack"]  # ack only, no card update


async def test_reject_denies_and_never_dispatches() -> None:
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        runner = _FakeRunner(_parsed(verb="rej", deliverable_id=str(deliverable_id)))
        handled = await handle_approval_callback(
            adapter=_adapter(),
            raw_body=_raw(verb="rej", deliverable_id=str(deliverable_id)),
            account=_account(ws),
            session=session,
            plugin=object(),
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
            runner=runner,  # type: ignore[arg-type]
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.DENIED
    assert dispatcher.calls == []
    assert [a[0] for a in runner.actions] == ["ack", "update"]


async def test_double_tap_on_settled_item_only_acks_no_update() -> None:
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        row = await session.get(SafeModeQueueItemRow, item_id)
        row.status = SafeModeStatus.APPROVED
        await session.commit()
        runner = _FakeRunner(_parsed(verb="apv", deliverable_id=str(deliverable_id)))
        handled = await handle_approval_callback(
            adapter=_adapter(),
            raw_body=_raw(verb="apv", deliverable_id=str(deliverable_id)),
            account=_account(ws),
            session=session,
            plugin=object(),
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
            runner=runner,  # type: ignore[arg-type]
        )
        assert handled is True
    assert dispatcher.calls == []
    assert [a[0] for a in runner.actions] == ["ack"]  # already-handled ack, NO re-update
