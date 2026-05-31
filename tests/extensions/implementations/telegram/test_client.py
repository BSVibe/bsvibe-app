"""Tests for TelegramClient — the httpx wrapper. httpx is mocked via respx;
no real Telegram calls. (rule python-testing: never call real APIs in tests.)

Telegram returns HTTP 200 with ``{"ok": false, "description": ...}`` on
logical failure, so the client must NOT treat a 200 as unconditional success."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.extensions.implementations.telegram.client import TelegramApiError, TelegramClient

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
