"""Tests for DiscordClient — the httpx wrapper. httpx is mocked via respx; no
real Discord calls. (rule python-testing: never call real APIs in tests.)

Discord signals failure with a non-2xx HTTP status + JSON error body, so the
client raises DiscordApiError on non-2xx and treats 404-on-delete as an
idempotent no-op."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.plugins.implementations.discord.client import (
    DEFAULT_BASE_URL,
    DiscordApiError,
    DiscordClient,
)

API = "https://discord.com/api/v10"
TOKEN = "bot-token-abc"
CHANNEL = "555"


@pytest.fixture
def client() -> DiscordClient:
    return DiscordClient(TOKEN, base_url=API)


class TestCreateMessage:
    @respx.mock
    async def test_create_message_posts_and_returns_result(self, client):
        route = respx.post(f"{API}/channels/{CHANNEL}/messages").mock(
            return_value=httpx.Response(
                200, json={"id": "42", "channel_id": CHANNEL, "content": "hello"}
            )
        )
        data = await client.create_message(CHANNEL, "hello")
        assert data["id"] == "42"
        assert route.called
        sent = route.calls.last.request
        # Token rides in the Authorization: Bot header, never the URL/query.
        assert sent.headers["authorization"] == f"Bot {TOKEN}"
        assert TOKEN not in str(sent.url)
        assert b'"content"' in sent.content
        assert b"hello" in sent.content

    @respx.mock
    async def test_create_message_accepts_201(self, client):
        # Discord returns 200 for message creation, but accept any 2xx.
        respx.post(f"{API}/channels/{CHANNEL}/messages").mock(
            return_value=httpx.Response(201, json={"id": "9", "channel_id": CHANNEL})
        )
        data = await client.create_message(CHANNEL, "hi")
        assert data["id"] == "9"

    @respx.mock
    async def test_create_message_non_2xx_raises(self, client):
        respx.post(f"{API}/channels/{CHANNEL}/messages").mock(
            return_value=httpx.Response(403, json={"message": "Missing Permissions", "code": 50013})
        )
        with pytest.raises(DiscordApiError, match="Missing Permissions") as exc:
            await client.create_message(CHANNEL, "hello")
        assert exc.value.status == 403

    @respx.mock
    async def test_create_message_non_json_error_uses_text(self, client):
        respx.post(f"{API}/channels/{CHANNEL}/messages").mock(
            return_value=httpx.Response(500, text="upstream boom")
        )
        with pytest.raises(DiscordApiError, match="upstream boom") as exc:
            await client.create_message(CHANNEL, "hi")
        assert exc.value.status == 500


class TestDeleteMessage:
    @respx.mock
    async def test_delete_message_success_returns_none(self, client):
        respx.delete(f"{API}/channels/{CHANNEL}/messages/42").mock(return_value=httpx.Response(204))
        result = await client.delete_message(CHANNEL, "42")
        assert result is None

    @respx.mock
    async def test_delete_message_already_gone_returns_404(self, client):
        # An already-deleted message is a no-op: surface the 404 status so the
        # compensate handler can treat it as success rather than raising.
        respx.delete(f"{API}/channels/{CHANNEL}/messages/42").mock(
            return_value=httpx.Response(404, json={"message": "Unknown Message", "code": 10008})
        )
        result = await client.delete_message(CHANNEL, "42")
        assert result == 404

    @respx.mock
    async def test_delete_message_other_error_raises(self, client):
        respx.delete(f"{API}/channels/{CHANNEL}/messages/42").mock(
            return_value=httpx.Response(403, json={"message": "Missing Permissions"})
        )
        with pytest.raises(DiscordApiError, match="Missing Permissions"):
            await client.delete_message(CHANNEL, "42")


class TestInjectedClient:
    @respx.mock
    async def test_uses_injected_async_client(self):
        respx.post(f"{API}/channels/{CHANNEL}/messages").mock(
            return_value=httpx.Response(200, json={"id": "7", "channel_id": CHANNEL})
        )
        async with httpx.AsyncClient() as injected:
            dc = DiscordClient(TOKEN, base_url=API, client=injected)
            data = await dc.create_message(CHANNEL, "hi")
        assert data["id"] == "7"

    def test_default_base_url(self):
        assert DEFAULT_BASE_URL == "https://discord.com/api/v10"
