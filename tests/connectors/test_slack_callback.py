"""Inbound Slack block_actions handler — the SECURITY-critical approve path.

A founder taps 승인 / 거절 on the "작업 완료" Block Kit card; this handler settles the
held Safe-Mode item straight from Slack. Slack delivers to a CHANNEL, so ANY
member can click — the tests here pin, above all, that a tap is only honoured for
an AUTHORIZED user (``delivery_config['authorized_user_ids']`` allowlist, with a
FAIL-CLOSED empty allowlist) and, when a ``team_id`` is bound, only from that
workspace. A non-allowlisted tap, an empty allowlist, a wrong team, and a crafted
cross-workspace deliverable id must NOT approve anything.

Slack HTTP is mocked via respx; the outbound dispatcher is a fake; the bot token
decryption is a fake cipher. No real Slack calls, no real dispatch.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import respx

from backend.connectors.db import ConnectorAccountRow
from backend.connectors.slack_callback import handle_slack_callback, process_slack_callback
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.domain.delivery import DeliveryResult
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus
from plugin.slack import plugin as slack_module
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

API = "https://slack.com/api"
TOKEN = "xoxb-TESTTOKEN"  # noqa: S105 — test fixture
SLACK = slack_module.p.meta

FOUNDER_USER = "U_FOUNDER"
TEAM = "T_WS"
CHANNEL = "C_CARD"
MESSAGE_TS = "1700000000.000100"
RESPONSE_URL = "https://hooks.slack.com/actions/T_WS/123456/abcdef"

# The Block Kit card a "작업 완료" deliverable was sent with: a section (body, with a
# <url|보고서 보기> mrkdwn link) + an actions block carrying the 승인/거절 buttons.
CARD_SECTION = {
    "type": "section",
    "text": {
        "type": "mrkdwn",
        "text": "작업 완료\n\n검증까지 끝났어요.\n\n<https://x/deliverables/1|보고서 보기>",
    },
}


def _card_blocks(deliverable_id: str) -> list[dict[str, Any]]:
    return [
        CARD_SECTION,
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "승인"},
                    "action_id": f"apv:{deliverable_id}",
                    "value": f"apv:{deliverable_id}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "거절"},
                    "action_id": f"rej:{deliverable_id}",
                    "value": f"rej:{deliverable_id}",
                },
            ],
        },
    ]


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
    team_id: str | None = None,
) -> ConnectorAccountRow:
    delivery_config: dict[str, Any] = {"channel": CHANNEL}
    if authorized_user_ids is not None:
        delivery_config["authorized_user_ids"] = authorized_user_ids
    if team_id is not None:
        delivery_config["team_id"] = team_id
    return ConnectorAccountRow(
        workspace_id=ws,
        connector="slack",
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
    team_id: str = TEAM,
    with_blocks: bool = True,
) -> dict[str, Any]:
    message: dict[str, Any] = {"ts": MESSAGE_TS}
    if with_blocks:
        message["blocks"] = _card_blocks(deliverable_id)
    return {
        "type": "block_actions",
        "user": {"id": user_id, "team_id": team_id},
        "team": {"id": team_id},
        "channel": {"id": CHANNEL},
        "message": message,
        "response_url": RESPONSE_URL,
        "actions": [
            {
                "type": "button",
                "action_id": f"{verb}:{deliverable_id}",
                "value": f"{verb}:{deliverable_id}",
            }
        ],
    }


def _raw(**kwargs: Any) -> bytes:
    """The raw_body the shared handler receives: the block_actions JSON (already
    form-decoded by ``process_slack_callback``)."""
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


def _mock_ephemeral() -> respx.Route:
    return respx.post(RESPONSE_URL).mock(return_value=httpx.Response(200, text="ok"))


def _mock_update() -> respx.Route:
    return respx.post(f"{API}/chat.update").mock(
        return_value=httpx.Response(200, json={"ok": True, "channel": CHANNEL, "ts": MESSAGE_TS})
    )


# ── SECURITY: non-authorized taps must NOT approve ─────────────────────────────


@respx.mock
async def test_non_authorized_user_is_rejected_and_item_stays_pending() -> None:
    """THE critical test: a tap from a user id NOT in ``authorized_user_ids`` must
    not approve — the item stays PENDING, no dispatch, an ephemeral 권한 없음 note."""
    ephemeral = _mock_ephemeral()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_slack_callback(
            raw_body=_raw(deliverable_id=str(deliverable_id), user_id="U_STRANGER"),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            slack=SLACK,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.PENDING
    assert dispatcher.calls == []  # NO approve dispatch
    assert ephemeral.called
    assert "권한" in _last_body(ephemeral)["text"]


@respx.mock
async def test_empty_authorized_user_ids_is_fail_closed_no_approve() -> None:
    """An empty/missing allowlist → FAIL-CLOSED: nobody is authorized (approval is
    irreversible). Even the 'right-looking' user does not approve."""
    ephemeral = _mock_ephemeral()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        # Empty list AND missing key are both fail-closed.
        for account in (_account(ws, authorized_user_ids=[]), _account(ws)):
            handled = await handle_slack_callback(
                raw_body=_raw(deliverable_id=str(deliverable_id)),
                account=account,
                session=session,
                slack=SLACK,
                cipher=_FakeCipher(),
                dispatcher=dispatcher,
            )
            assert handled is True
            assert await _status(session, item_id) is SafeModeStatus.PENDING
    assert dispatcher.calls == []
    assert ephemeral.called


@respx.mock
async def test_wrong_team_id_is_rejected_when_team_bound() -> None:
    """When ``delivery_config['team_id']`` is set, a tap from another workspace
    (different ``team.id``) is rejected even if the user id is allowlisted."""
    ephemeral = _mock_ephemeral()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_slack_callback(
            raw_body=_raw(deliverable_id=str(deliverable_id), team_id="T_OTHER"),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER], team_id=TEAM),
            session=session,
            slack=SLACK,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.PENDING
    assert dispatcher.calls == []
    assert ephemeral.called


@respx.mock
async def test_cross_workspace_deliverable_id_finds_nothing_no_approve() -> None:
    """A crafted deliverable_id belonging to ANOTHER workspace resolves to no
    pending item in the account's workspace → treated as already-handled, and the
    other tenant's item is NEVER approved."""
    ephemeral = _mock_ephemeral()
    _mock_update()
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

        handled = await handle_slack_callback(
            raw_body=_raw(deliverable_id=str(other_deliverable)),
            account=_account(ws_a, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            slack=SLACK,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, other_item) is SafeModeStatus.PENDING
    assert dispatcher.calls == []
    assert "이미" in _last_body(ephemeral)["text"]


# ── happy path: authorized founder ─────────────────────────────────────────────


@respx.mock
async def test_authorized_approve_dispatches_and_updates_keeping_body() -> None:
    _mock_ephemeral()
    update = _mock_update()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_slack_callback(
            raw_body=_raw(verb="apv", deliverable_id=str(deliverable_id)),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            slack=SLACK,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.APPROVED
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0]["deliverable_id"] == deliverable_id
    body = _last_body(update)
    assert body["channel"] == CHANNEL
    assert body["ts"] == MESSAGE_TS
    blocks = body["blocks"]
    # HISTORY: the ORIGINAL non-button block (the card body) is KEPT.
    assert CARD_SECTION in blocks
    # The actions (buttons) block is DROPPED.
    assert all(b.get("type") != "actions" for b in blocks)
    # A status block is APPENDED.
    flat = json.dumps(blocks, ensure_ascii=False)
    assert "✅ 승인됨 — 내보냈어요." in flat
    # A text fallback accompanies the blocks (accessibility / notifications).
    assert body.get("text")


@respx.mock
async def test_authorized_reject_denies_and_updates() -> None:
    _mock_ephemeral()
    update = _mock_update()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_slack_callback(
            raw_body=_raw(verb="rej", deliverable_id=str(deliverable_id)),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            slack=SLACK,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.DENIED
    assert dispatcher.calls == []  # deny never dispatches
    blocks = _last_body(update)["blocks"]
    assert CARD_SECTION in blocks
    assert all(b.get("type") != "actions" for b in blocks)
    assert "❌ 거절했어요." in json.dumps(blocks, ensure_ascii=False)


@respx.mock
async def test_update_http_error_does_not_fail_the_settled_callback() -> None:
    """After approve COMMITS, Slack returns ok:false on chat.update. The callback
    must STILL settle (handled=True, item APPROVED, dispatch fired), not 500."""
    _mock_ephemeral()
    respx.post(f"{API}/chat.update").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "message_not_found"})
    )
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        handled = await handle_slack_callback(
            raw_body=_raw(verb="apv", deliverable_id=str(deliverable_id)),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            slack=SLACK,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.APPROVED
    assert len(dispatcher.calls) == 1


@respx.mock
async def test_double_tap_already_resolved_is_idempotent() -> None:
    ephemeral = _mock_ephemeral()
    update = _mock_update()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        row = await session.get(SafeModeQueueItemRow, item_id)
        row.status = SafeModeStatus.APPROVED
        await session.commit()

        handled = await handle_slack_callback(
            raw_body=_raw(verb="apv", deliverable_id=str(deliverable_id)),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            slack=SLACK,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.APPROVED
    assert dispatcher.calls == []
    assert "이미" in _last_body(ephemeral)["text"]
    # Already edited on the FIRST tap — a double-tap must NOT re-edit.
    assert not update.called


# ── routing discriminator + form decode ─────────────────────────────────────────


async def test_non_interaction_body_returns_false_for_fall_through() -> None:
    """A non-block_actions body → the handler reports it did not handle an
    interaction, so the route falls through."""
    async with memory_session() as session:
        ws = uuid.uuid4()
        handled = await handle_slack_callback(
            raw_body=json.dumps({"type": "event_callback", "event": {}}).encode(),
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            slack=SLACK,
            cipher=_FakeCipher(),
            dispatcher=_FakeDispatcher(),
        )
        assert handled is False


@respx.mock
async def test_process_slack_callback_decodes_form_encoded_payload() -> None:
    """The route-facing entrypoint decodes the ``payload=<json>`` form body Slack
    POSTs, then settles the held item for the authorized user."""
    import urllib.parse

    _mock_ephemeral()
    _mock_update()
    dispatcher = _FakeDispatcher()
    async with memory_session() as session:
        ws = uuid.uuid4()
        item_id, deliverable_id = await _seed(session, ws=ws)
        form = urllib.parse.urlencode(
            {"payload": json.dumps(_payload(deliverable_id=str(deliverable_id)))}
        ).encode()
        # process_slack_callback loads the real plugin meta via importlib.
        handled = await process_slack_callback(
            raw_body=form,
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            cipher=_FakeCipher(),
            dispatcher=dispatcher,
        )
        assert handled is True
        assert await _status(session, item_id) is SafeModeStatus.APPROVED
    assert len(dispatcher.calls) == 1


async def test_process_slack_callback_non_form_body_returns_false() -> None:
    """A raw (non form-encoded) body → nothing to decode → route falls through."""
    async with memory_session() as session:
        ws = uuid.uuid4()
        handled = await process_slack_callback(
            raw_body=b'{"type":"event_callback"}',
            account=_account(ws, authorized_user_ids=[FOUNDER_USER]),
            session=session,
            cipher=_FakeCipher(),
        )
        assert handled is False
