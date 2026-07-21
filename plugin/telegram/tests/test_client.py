"""Tests for TelegramClient — the httpx wrapper. httpx is mocked via respx;
no real Telegram calls. (rule python-testing: never call real APIs in tests.)

Telegram returns HTTP 200 with ``{"ok": false, "description": ...}`` on
logical failure, so the client must NOT treat a 200 as unconditional success."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from plugin.telegram.client import TelegramApiError, TelegramClient

API = "https://api.telegram.org"
TOKEN = "12345:abcdef"
BOT = f"{API}/bot{TOKEN}"


@pytest.fixture
def client() -> TelegramClient:
    return TelegramClient(TOKEN, base_url=API)


class TestSendMessage:
    @respx.mock
    async def test_send_message_posts_and_returns_result(self, client):
        route = respx.post(f"{BOT}/sendMessage").mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": 42, "chat": {"id": 99}}},
            )
        )
        data = await client.send_message(99, "hello")
        assert data["message_id"] == 42
        assert route.called
        sent = route.calls.last.request
        # Token is in the URL path, never a header.
        assert TOKEN in str(sent.url)
        assert b'"chat_id"' in sent.content
        assert b"hello" in sent.content

    @respx.mock
    async def test_send_message_string_chat_id(self, client):
        route = respx.post(f"{BOT}/sendMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": 1}}}
            )
        )
        await client.send_message("@channelname", "hi")
        assert b"@channelname" in route.calls.last.request.content

    @respx.mock
    async def test_send_message_ok_false_raises(self, client):
        # Telegram signals logical failure with HTTP 200 + ok:false.
        respx.post(f"{BOT}/sendMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": False, "description": "Bad Request: chat not found"}
            )
        )
        with pytest.raises(TelegramApiError, match="chat not found"):
            await client.send_message(99, "hello")

    @respx.mock
    async def test_send_message_raises_on_http_error(self, client):
        respx.post(f"{BOT}/sendMessage").mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(httpx.HTTPStatusError):
            await client.send_message(99, "hello")


class TestDeleteMessage:
    @respx.mock
    async def test_delete_message_success_returns_none(self, client):
        respx.post(f"{BOT}/deleteMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )
        result = await client.delete_message(99, 42)
        assert result is None

    @respx.mock
    async def test_delete_message_already_gone_returns_description(self, client):
        # An already-deleted message is a no-op: surface the description so the
        # compensate handler can treat it as success rather than raising.
        respx.post(f"{BOT}/deleteMessage").mock(
            return_value=httpx.Response(
                200,
                json={"ok": False, "description": "Bad Request: message to delete not found"},
            )
        )
        result = await client.delete_message(99, 42)
        assert result is not None
        assert "message to delete not found" in result.lower()

    @respx.mock
    async def test_delete_message_other_error_raises(self, client):
        respx.post(f"{BOT}/deleteMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": False, "description": "Forbidden: bot was blocked"}
            )
        )
        with pytest.raises(TelegramApiError, match="blocked"):
            await client.delete_message(99, 42)


class TestSendMessageReplyMarkup:
    @respx.mock
    async def test_send_message_includes_reply_markup_when_set(self, client):
        route = respx.post(f"{BOT}/sendMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": 1}}}
            )
        )
        markup = {"inline_keyboard": [[{"text": "승인", "callback_data": "apv:x"}]]}
        await client.send_message(1, "hi", reply_markup=markup)
        body = json.loads(route.calls.last.request.content)
        assert body["reply_markup"] == markup

    @respx.mock
    async def test_send_message_omits_reply_markup_when_none(self, client):
        route = respx.post(f"{BOT}/sendMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": 1}}}
            )
        )
        await client.send_message(1, "hi")
        body = json.loads(route.calls.last.request.content)
        assert "reply_markup" not in body


class TestAnswerCallbackQuery:
    @respx.mock
    async def test_posts_endpoint_and_body(self, client):
        route = respx.post(f"{BOT}/answerCallbackQuery").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )
        await client.answer_callback_query("cbid", text="권한이 없어요")
        assert route.called
        body = json.loads(route.calls.last.request.content)
        assert body["callback_query_id"] == "cbid"
        assert body["text"] == "권한이 없어요"
        assert TOKEN in str(route.calls.last.request.url)

    @respx.mock
    async def test_omits_text_when_none(self, client):
        route = respx.post(f"{BOT}/answerCallbackQuery").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )
        await client.answer_callback_query("cbid")
        body = json.loads(route.calls.last.request.content)
        assert "text" not in body

    @respx.mock
    async def test_tolerates_ok_false_without_raising(self, client):
        # Answering an expired callback query id ("query is too old") is a benign,
        # best-effort UI ack — it must NOT raise (the approve already happened).
        respx.post(f"{BOT}/answerCallbackQuery").mock(
            return_value=httpx.Response(
                200, json={"ok": False, "description": "Bad Request: query is too old"}
            )
        )
        # no exception
        await client.answer_callback_query("cbid")

    @respx.mock
    async def test_tolerates_http_400_without_raising(self, client):
        # Telegram returns HTTP 400 (not 200+ok:false) for an expired/invalid
        # query id. This best-effort UI ack must NOT raise — the state change the
        # tap triggered already committed; a 400 here must not 500 the webhook.
        respx.post(f"{BOT}/answerCallbackQuery").mock(
            return_value=httpx.Response(
                400, json={"ok": False, "description": "Bad Request: query is too old"}
            )
        )
        # no exception
        await client.answer_callback_query("cbid")


class TestEditMessageText:
    @respx.mock
    async def test_posts_endpoint_and_body(self, client):
        route = respx.post(f"{BOT}/editMessageText").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {}})
        )
        markup = {"inline_keyboard": []}
        await client.edit_message_text(99, 42, "✅ 승인됨", reply_markup=markup)
        body = json.loads(route.calls.last.request.content)
        assert body["chat_id"] == 99
        assert body["message_id"] == 42
        assert body["text"] == "✅ 승인됨"
        assert body["reply_markup"] == markup

    @respx.mock
    async def test_omits_reply_markup_when_none(self, client):
        route = respx.post(f"{BOT}/editMessageText").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {}})
        )
        await client.edit_message_text(99, 42, "hi")
        body = json.loads(route.calls.last.request.content)
        assert "reply_markup" not in body

    @respx.mock
    async def test_tolerates_ok_false_without_raising(self, client):
        respx.post(f"{BOT}/editMessageText").mock(
            return_value=httpx.Response(
                200, json={"ok": False, "description": "Bad Request: message is not modified"}
            )
        )
        await client.edit_message_text(99, 42, "hi")

    @respx.mock
    async def test_tolerates_http_400_without_raising(self, client):
        respx.post(f"{BOT}/editMessageText").mock(
            return_value=httpx.Response(
                400, json={"ok": False, "description": "Bad Request: message to edit not found"}
            )
        )
        # no exception
        await client.edit_message_text(99, 42, "hi")


class TestInjectedClient:
    @respx.mock
    async def test_uses_injected_async_client(self):
        respx.post(f"{BOT}/sendMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"message_id": 7, "chat": {"id": 1}}}
            )
        )
        async with httpx.AsyncClient() as injected:
            tc = TelegramClient(TOKEN, base_url=API, client=injected)
            data = await tc.send_message(1, "hi")
        assert data["message_id"] == 7
