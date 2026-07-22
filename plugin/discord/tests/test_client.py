"""Tests for DiscordClient — the httpx wrapper. httpx is mocked via respx; no
real Discord calls. (rule python-testing: never call real APIs in tests.)

Discord signals failure with a non-2xx HTTP status + JSON error body, so the
client raises DiscordApiError on non-2xx and treats 404-on-delete as an
idempotent no-op."""

from __future__ import annotations

import httpx
import pytest
import respx

from plugin.discord.client import (
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


APP = "APP123"
ITOKEN = "itoken-xyz"


class TestCreateMessageComponents:
    @respx.mock
    async def test_components_included_only_when_set(self, client):
        route = respx.post(f"{API}/channels/{CHANNEL}/messages").mock(
            return_value=httpx.Response(200, json={"id": "1", "channel_id": CHANNEL})
        )
        await client.create_message(CHANNEL, "hi")
        assert b"components" not in route.calls.last.request.content

        rows = [{"type": 1, "components": [{"type": 2, "style": 3, "label": "승인"}]}]
        await client.create_message(CHANNEL, "hi", components=rows)
        assert b"components" in route.calls.last.request.content


class TestInteractionWebhook:
    @respx.mock
    async def test_followup_posts_to_interaction_webhook(self, client):
        route = respx.post(f"{API}/webhooks/{APP}/{ITOKEN}").mock(
            return_value=httpx.Response(200, json={"id": "fu1"})
        )
        data = await client.create_interaction_followup(APP, ITOKEN, "권한이 없어요.", flags=64)
        assert data["id"] == "fu1"
        assert route.called
        import json as _json

        payload = _json.loads(route.calls.last.request.content)
        assert payload["content"] == "권한이 없어요."
        assert payload["flags"] == 64

    @respx.mock
    async def test_followup_omits_flags_when_unset(self, client):
        route = respx.post(f"{API}/webhooks/{APP}/{ITOKEN}").mock(
            return_value=httpx.Response(200, json={"id": "fu2"})
        )
        await client.create_interaction_followup(APP, ITOKEN, "hi")
        assert b"flags" not in route.calls.last.request.content

    @respx.mock
    async def test_edit_patches_original_interaction_response(self, client):
        route = respx.patch(f"{API}/webhooks/{APP}/{ITOKEN}/messages/@original").mock(
            return_value=httpx.Response(200, json={"id": "M1", "content": "done"})
        )
        data = await client.edit_interaction_response(
            APP, ITOKEN, "작업 완료\n\n✅ 승인됨", components=[]
        )
        assert data["id"] == "M1"
        assert route.called
        import json as _json

        payload = _json.loads(route.calls.last.request.content)
        assert payload["content"] == "작업 완료\n\n✅ 승인됨"
        assert payload["components"] == []

    @respx.mock
    async def test_edit_error_raises(self, client):
        respx.patch(f"{API}/webhooks/{APP}/{ITOKEN}/messages/@original").mock(
            return_value=httpx.Response(500, json={"message": "boom"})
        )
        with pytest.raises(DiscordApiError, match="boom"):
            await client.edit_interaction_response(APP, ITOKEN, "x")
