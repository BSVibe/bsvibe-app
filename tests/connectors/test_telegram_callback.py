"""Inbound Telegram callback_query handler — the SECURITY-critical approve path.

A founder taps Approve / Reject on the "작업 완료" card; this handler settles the
held Safe-Mode item straight from Telegram. The tests here pin, above all, that a
tap is only honoured for the AUTHORIZED founder (private chat + ``from.id`` equals
the account's bound ``chat_id``): a non-founder tap, a group-chat tap, and a
crafted cross-workspace deliverable id must NOT approve anything.

Telegram HTTP is mocked via respx; the outbound dispatcher is a fake; the bot
token decryption is a fake cipher. No real Telegram calls, no real dispatch.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import respx

from backend.connectors.db import ConnectorAccountRow
from backend.connectors.telegram_callback import handle_telegram_callback
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.domain.delivery import DeliveryResult
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus
from plugin.telegram import plugin as telegram_module
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

API = "https://api.telegram.org"
TOKEN = "TESTTOKEN"
BOT = f"{API}/bot{TOKEN}"
TELEGRAM = telegram_module.p.meta

FOUNDER_ID = 424242


class _FakeCipher:
    """Duck-typed CredentialCipher — decrypts any ciphertext to the bot token."""

    def decrypt(self, token: str) -> str:  # noqa: ARG002
        return TOKEN


class _FakeDispatcher:
    """Records approve-time dispatch calls; returns an empty DeliveryResult."""

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
        self.calls.append({"workspace_id": workspace_id, "deliverable_id": deliverable_id})
        return DeliveryResult(
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,  # type: ignore[arg-type]
            actions=[],
        )


def _account(ws: uuid.UUID, *, chat_id: str | int = FOUNDER_ID) -> ConnectorAccountRow:
    return ConnectorAccountRow(
        workspace_id=ws,
        connector="telegram",
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="ciphertext",
        delivery_config={"chat_id": chat_id},
        is_active=True,
    )


def _callback_raw(
    *,
    verb: str = "apv",
    deliverable_id: str,
    from_id: int = FOUNDER_ID,
    chat_type: str = "private",
) -> bytes:
    return json.dumps(
        {
            "update_id": 999,
            "callback_query": {
                "id": "cbq-77",
                "from": {"id": from_id, "is_bot": False},
                "message": {
                    "message_id": 555,
                    "chat": {"id": FOUNDER_ID, "type": chat_type},
                },
                "data": f"{verb}:{deliverable_id}",
            },
        }
    ).encode()


async def _seed(session, *, ws: uuid.UUID, language: str = "ko") -> tuple[uuid.UUID, uuid.UUID]:
    """Workspace + owner membership + one PENDING held delivery. Returns
    ``(item_id, deliverable_id)``."""
    owner = UserRow(id=uuid.uuid4(), supabase_user_id=f"sub-{uuid.uuid4().hex}")
    session.add(WorkspaceRow(id=ws, name="WS", language=language))
    session.add(owner)
    session.add(MembershipRow(user_id=owner.id, workspace_id=ws, role="owner"))
    deliverable_id = uuid.uuid4()
    queue = SafeModeQueue(session)
    item_id = await queue.enqueue(workspace_id=ws, deliverable_id=deliverable_id)
    await session.commit()
    return item_id, deliverable_id


async def _status(session, item_id: uuid.UUID) -> SafeModeStatus:
    session.expire_all()
    row = await session.get(SafeModeQueueItemRow, item_id)
    assert row is not None
    return row.status


def _last_body(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)


# ── SECURITY: non-founder taps must NOT approve ────────────────────────────────


@respx.mock
async def test_non_founder_from_id_is_rejected_and_item_stays_pending() -> None:
    """THE critical test: a tap from an id that is NOT the bound founder chat_id
    must not approve — the item stays PENDING, no dispatch, a 권한 없음 ack."""
    answer = respx.post(f"{BOT}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_telegram_callback(
            raw_body=_callback_raw(deliverable_id=str(deliverable_id), from_id=999999),
            account=_account(ws),
            session=session,
            telegram=TELEGRAM,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.PENDING
    assert dispatcher.calls == []  # NO approve dispatch
    assert answer.called
    assert "권한" in _last_body(answer)["text"]


@respx.mock
async def test_group_chat_tap_is_rejected_and_item_stays_pending() -> None:
    """A tap in a non-private chat (a group any member could tap) must not
    approve, even if from.id happened to match."""
    answer = respx.post(f"{BOT}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_telegram_callback(
            raw_body=_callback_raw(
                deliverable_id=str(deliverable_id), from_id=FOUNDER_ID, chat_type="group"
            ),
            account=_account(ws),
            session=session,
            telegram=TELEGRAM,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.PENDING
    assert dispatcher.calls == []
    assert answer.called
    assert "권한" in _last_body(answer)["text"]


@respx.mock
async def test_cross_workspace_deliverable_id_finds_nothing_no_approve() -> None:
    """A crafted deliverable_id belonging to ANOTHER workspace resolves to no
    pending item in the account's workspace → treated as already-handled, and the
    other tenant's item is NEVER approved."""
    answer = respx.post(f"{BOT}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    respx.post(f"{BOT}/editMessageText").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws_a = uuid.uuid4()
        ws_b = uuid.uuid4()
        await _seed(session, ws=ws_a)  # the founder's own workspace (owner set)
        # A pending item that belongs to a DIFFERENT workspace.
        other_item = await SafeModeQueue(session).enqueue(
            workspace_id=ws_b, deliverable_id=uuid.uuid4()
        )
        other_deliverable = (await session.get(SafeModeQueueItemRow, other_item)).deliverable_id
        await session.commit()

        handled = await handle_telegram_callback(
            raw_body=_callback_raw(deliverable_id=str(other_deliverable), from_id=FOUNDER_ID),
            account=_account(ws_a),
            session=session,
            telegram=TELEGRAM,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        # The other tenant's item is untouched.
        assert await _status(session, other_item) is SafeModeStatus.PENDING
    assert dispatcher.calls == []
    assert "이미" in _last_body(answer)["text"]


# ── happy path: authorized founder ─────────────────────────────────────────────


@respx.mock
async def test_valid_founder_approve_dispatches_and_edits() -> None:
    answer = respx.post(f"{BOT}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    edit = respx.post(f"{BOT}/editMessageText").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_telegram_callback(
            raw_body=_callback_raw(verb="apv", deliverable_id=str(deliverable_id)),
            account=_account(ws),
            session=session,
            telegram=TELEGRAM,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.APPROVED
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0]["deliverable_id"] == deliverable_id
    assert answer.called
    assert "✅" in _last_body(edit)["text"]
    # buttons dropped
    assert _last_body(edit)["reply_markup"] == {"inline_keyboard": []}


@respx.mock
async def test_valid_founder_reject_denies_and_edits() -> None:
    respx.post(f"{BOT}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    edit = respx.post(f"{BOT}/editMessageText").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_telegram_callback(
            raw_body=_callback_raw(verb="rej", deliverable_id=str(deliverable_id)),
            account=_account(ws),
            session=session,
            telegram=TELEGRAM,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.DENIED
    assert dispatcher.calls == []  # deny never dispatches
    assert "❌" in _last_body(edit)["text"]


@respx.mock
async def test_double_tap_already_resolved_is_idempotent() -> None:
    """A second tap after the item already settled → friendly '이미 처리됐어요',
    no error, no second approve/dispatch."""
    answer = respx.post(f"{BOT}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    respx.post(f"{BOT}/editMessageText").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        # Simulate the item already approved by a prior tap.
        row = await session.get(SafeModeQueueItemRow, item_id)
        row.status = SafeModeStatus.APPROVED
        await session.commit()

        handled = await handle_telegram_callback(
            raw_body=_callback_raw(verb="apv", deliverable_id=str(deliverable_id)),
            account=_account(ws),
            session=session,
            telegram=TELEGRAM,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.APPROVED
    assert dispatcher.calls == []
    assert "이미" in _last_body(answer)["text"]


# ── routing discriminator ──────────────────────────────────────────────────────


async def test_non_callback_body_returns_false_for_fall_through() -> None:
    """A non-callback body (e.g. a plain message that skipped) → the handler
    reports it did not handle a callback, so the route falls through."""
    async with memory_session() as session:
        ws = uuid.uuid4()
        handled = await handle_telegram_callback(
            raw_body=json.dumps({"update_id": 1, "message": {}}).encode(),
            account=_account(ws),
            session=session,
            telegram=TELEGRAM,
            cipher=_FakeCipher(),
            dispatcher=_FakeDispatcher(),
        )
        assert handled is False
