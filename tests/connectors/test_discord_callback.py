"""Inbound Discord component-interaction handler — the SECURITY + TIMING path.

A founder taps 승인 / 거절 on the "작업 완료" message-component card; this handler
settles the held Safe-Mode item straight from Discord. Discord delivers a card to
a CHANNEL, so ANY member can click — the tests here pin, above all, that a tap is
only honoured for an AUTHORIZED user (``delivery_config['authorized_user_ids']``
allowlist, FAIL-CLOSED when empty) and, when a ``guild_id`` is bound, only from
that guild. A non-allowlisted tap, an empty allowlist, a wrong guild, and a
crafted cross-workspace deliverable id must NOT approve anything.

TIMING: Discord requires the interactions endpoint to answer within ~3s, but our
approve + dispatch (opens a GitHub PR) can take longer. ``process_discord_callback``
therefore returns a synchronous DEFERRED (type 6) response and schedules the slow
approve/dispatch/edit on a Starlette ``BackgroundTask`` that opens a FRESH DB
session. The timing test drives exactly that: assert the type-6 body, then invoke
the scheduled callable and assert its effects on a fresh session.

Discord HTTP is mocked via respx; the outbound dispatcher is a fake; the bot token
decryption is a fake cipher. No real Discord calls, no real dispatch.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import respx
from fastapi.responses import JSONResponse

from backend.connectors.db import ConnectorAccountRow
from backend.connectors.discord_callback import (
    handle_discord_callback,
    process_discord_callback,
)
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.domain.delivery import DeliveryResult
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus
from plugin.discord import plugin as discord_module
from tests._support import memory_session, shared_file_sessionmaker

pytestmark = pytest.mark.asyncio

API = "https://discord.com/api/v10"
TOKEN = "bot-token-abc"  # noqa: S105 — test fixture
DISCORD = discord_module.p.meta

FOUNDER_USER = "U_FOUNDER"
GUILD = "GUILD_WS"
CHANNEL = "CH_CARD"
MESSAGE_ID = "MSG1"
APP = "APP123"
ITOKEN = "itoken-xyz"  # noqa: S105 — test fixture (interaction token, URL capability)

# The original message-component card content: title, body, and a markdown
# [보고서 보기](url) link — the handler must KEEP this on the settle edit.
CARD_CONTENT = "작업 완료\n\n검증까지 끝났어요.\n\n[보고서 보기](https://x/deliverables/1)"


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


def _account(
    ws: uuid.UUID,
    *,
    authorized_user_ids: list[str] | None = None,
    guild_id: str | None = None,
) -> ConnectorAccountRow:
    delivery_config: dict[str, Any] = {"channel_id": CHANNEL}
    if authorized_user_ids is not None:
        delivery_config["authorized_user_ids"] = authorized_user_ids
    if guild_id is not None:
        delivery_config["guild_id"] = guild_id
    return ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        connector="discord",
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="ciphertext",
        delivery_config=delivery_config,
        is_active=True,
    )


def _payload(
    *,
    verb: str = "apv",
    deliverable_id: str,
    user_id: str = FOUNDER_USER,
    guild_id: str = GUILD,
    with_content: bool = True,
) -> dict[str, Any]:
    message: dict[str, Any] = {"id": MESSAGE_ID}
    if with_content:
        message["content"] = CARD_CONTENT
    return {
        "id": "interaction-1",
        "application_id": APP,
        "type": 3,  # MESSAGE_COMPONENT
        "token": ITOKEN,
        "guild_id": guild_id,
        "channel_id": CHANNEL,
        "member": {"user": {"id": user_id, "bot": False}},
        "message": message,
        "data": {"custom_id": f"{verb}:{deliverable_id}", "component_type": 2},
    }


def _raw(**kwargs: Any) -> bytes:
    return json.dumps(_payload(**kwargs)).encode()


async def _seed(session, *, ws: uuid.UUID, language: str = "ko") -> tuple[uuid.UUID, uuid.UUID]:
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


def _mock_followup() -> respx.Route:
    return respx.post(f"{API}/webhooks/{APP}/{ITOKEN}").mock(
        return_value=httpx.Response(200, json={"id": "fu1"})
    )


def _mock_edit() -> respx.Route:
    return respx.patch(f"{API}/webhooks/{APP}/{ITOKEN}/messages/@original").mock(
        return_value=httpx.Response(200, json={"id": MESSAGE_ID, "content": "x"})
    )


# ── SECURITY: non-authorized taps must NOT approve ─────────────────────────────


@respx.mock
async def test_non_authorized_user_gets_ephemeral_and_item_stays_pending() -> None:
    """THE critical test: a tap from a user id NOT in ``authorized_user_ids`` must
    not approve — the item stays PENDING, no dispatch, an EPHEMERAL 권한 없음 follow-up
    is sent (flags=64), and the public card is NOT edited (no @original PATCH)."""
    followup = _mock_followup()
    edit = _mock_edit()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_discord_callback(
            raw_body=_raw(deliverable_id=str(deliverable_id), user_id="U_STRANGER"),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            discord=DISCORD,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.PENDING
    assert dispatcher.calls == []  # NO approve dispatch
    assert followup.called
    body = _last_body(followup)
    assert "권한" in body["content"]
    assert body["flags"] == 64  # ephemeral — only the tapper sees it
    assert not edit.called  # public card untouched (buttons stay for the founder)


@respx.mock
async def test_empty_authorized_user_ids_is_fail_closed_no_approve() -> None:
    """An empty/missing allowlist → FAIL-CLOSED: nobody is authorized (approval is
    irreversible). Even the 'right-looking' user does not approve."""
    followup = _mock_followup()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        for account in (_account(ws, authorized_user_ids=[]), _account(ws)):
            handled = await handle_discord_callback(
                raw_body=_raw(deliverable_id=str(deliverable_id)),
                account=account,
                session=session,
                discord=DISCORD,
                cipher=_FakeCipher(),
                dispatcher=dispatcher,
            )
            assert handled is True
            assert await _status(session, item_id) is SafeModeStatus.PENDING
    assert dispatcher.calls == []
    assert followup.called


@respx.mock
async def test_wrong_guild_id_is_rejected_when_guild_bound() -> None:
    """When ``delivery_config['guild_id']`` is set, a tap from another guild is
    rejected even if the user id is allowlisted."""
    followup = _mock_followup()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_discord_callback(
            raw_body=_raw(deliverable_id=str(deliverable_id), guild_id="GUILD_OTHER"),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER], guild_id=GUILD),
            session=session,
            discord=DISCORD,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.PENDING
    assert dispatcher.calls == []
    assert followup.called


@respx.mock
async def test_cross_workspace_deliverable_id_finds_nothing_no_approve() -> None:
    """A crafted deliverable_id belonging to ANOTHER workspace resolves to no
    pending item in the account's workspace → treated as already-handled, and the
    other tenant's item is NEVER approved."""
    followup = _mock_followup()
    _mock_edit()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws_a = uuid.uuid4()
        ws_b = uuid.uuid4()
        await _seed(session, ws=ws_a)
        other_item = await SafeModeQueue(session).enqueue(
            workspace_id=ws_b, deliverable_id=uuid.uuid4()
        )
        other_deliverable = (await session.get(SafeModeQueueItemRow, other_item)).deliverable_id
        await session.commit()

        handled = await handle_discord_callback(
            raw_body=_raw(deliverable_id=str(other_deliverable)),
            account=_account(ws_a, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            discord=DISCORD,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, other_item) is SafeModeStatus.PENDING
    assert dispatcher.calls == []
    assert "이미" in _last_body(followup)["content"]


# ── happy path: authorized founder ─────────────────────────────────────────────


@respx.mock
async def test_authorized_approve_dispatches_and_edits_keeping_content() -> None:
    _mock_followup()
    edit = _mock_edit()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_discord_callback(
            raw_body=_raw(verb="apv", deliverable_id=str(deliverable_id)),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            discord=DISCORD,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.APPROVED
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0]["deliverable_id"] == deliverable_id
    body = _last_body(edit)
    # HISTORY: the ORIGINAL content (incl. the [보고서 보기](url) link) is KEPT.
    assert CARD_CONTENT in body["content"]
    # A status line is APPENDED.
    assert "✅ 승인됨 — 내보냈어요." in body["content"]
    # The buttons are DROPPED (empty components).
    assert body["components"] == []


@respx.mock
async def test_authorized_reject_denies_and_edits() -> None:
    _mock_followup()
    edit = _mock_edit()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_discord_callback(
            raw_body=_raw(verb="rej", deliverable_id=str(deliverable_id)),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            discord=DISCORD,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.DENIED
    assert dispatcher.calls == []  # deny never dispatches
    body = _last_body(edit)
    assert CARD_CONTENT in body["content"]
    assert "❌ 거절했어요." in body["content"]
    assert body["components"] == []


@respx.mock
async def test_edit_http_error_does_not_fail_the_settled_callback() -> None:
    """After approve COMMITS, Discord 500s on the @original edit. The callback must
    STILL settle (handled=True, item APPROVED, dispatch fired), not raise."""
    _mock_followup()
    respx.patch(f"{API}/webhooks/{APP}/{ITOKEN}/messages/@original").mock(
        return_value=httpx.Response(500, json={"message": "boom"})
    )
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_discord_callback(
            raw_body=_raw(verb="apv", deliverable_id=str(deliverable_id)),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            discord=DISCORD,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.APPROVED
    assert len(dispatcher.calls) == 1


async def test_non_interaction_body_returns_false_for_fall_through() -> None:
    """A non-component body (e.g. a PING) → the handler reports it did not handle an
    interaction, so the route falls through to the handshake/skip path."""
    async with memory_session() as session:
        ws = uuid.uuid4()
        handled = await handle_discord_callback(
            raw_body=json.dumps({"type": 1}).encode(),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            discord=DISCORD,
            cipher=_FakeCipher(),
            dispatcher=_FakeDispatcher(),
        )
        assert handled is False


# ── TIMING: type-6 synchronous response + approval on a background session ──────


@respx.mock
async def test_component_tap_responds_type6_and_runs_approval_on_background_session() -> None:
    """``process_discord_callback`` answers synchronously with DEFERRED (type 6) and
    schedules the slow approve/dispatch/edit as a background task that opens a FRESH
    DB session. Nothing is approved until the scheduled callable runs; invoking it
    settles the item + edits @original — all off the request session."""
    _mock_followup()
    edit = _mock_edit()
    dispatcher = _FakeDispatcher()
    async with shared_file_sessionmaker() as maker:
        ws = uuid.uuid4()
        async with maker() as s:
            item_id, deliverable_id = await _seed(s, ws=ws)
            account = _account(ws, authorized_user_ids=[FOUNDER_USER])
            s.add(account)
            await s.commit()

        async with maker() as req:
            result = await process_discord_callback(
                raw_body=_raw(deliverable_id=str(deliverable_id)),
                account=account,
                session=req,
                cipher=_FakeCipher(),
                dispatcher=dispatcher,
                session_factory=maker,
            )

        # Synchronous response is DEFERRED_UPDATE_MESSAGE (type 6), with a task.
        assert isinstance(result, JSONResponse)
        assert json.loads(result.body) == {"type": 6}
        assert result.background is not None

        # The approval has NOT happened yet — it is deferred to the background task.
        async with maker() as s:
            assert await _status(s, item_id) is SafeModeStatus.PENDING

        # Run the scheduled callable (Starlette invokes it after the response ships).
        await result.background()

        async with maker() as s:
            assert await _status(s, item_id) is SafeModeStatus.APPROVED

    assert len(dispatcher.calls) == 1
    assert edit.called
    body = _last_body(edit)
    assert body["components"] == []
    assert CARD_CONTENT in body["content"]
    assert "✅ 승인됨" in body["content"]


async def test_process_discord_callback_non_interaction_returns_false() -> None:
    """A PING (not a component tap) → ``process_discord_callback`` returns False so
    the route falls through to the handshake (PONG) path — no background task."""
    async with memory_session() as session:
        ws = uuid.uuid4()
        result = await process_discord_callback(
            raw_body=json.dumps({"type": 1}).encode(),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            cipher=_FakeCipher(),
        )
        assert result is False
