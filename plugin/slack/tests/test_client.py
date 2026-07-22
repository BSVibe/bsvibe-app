"""Tests for SlackClient — the httpx wrapper. httpx is mocked via respx;
no real Slack calls. (rule python-testing: never call real APIs in tests.)

Slack returns HTTP 200 with ``{"ok": false, "error": ...}`` on logical
failure, so the client must NOT treat a 200 as unconditional success."""

from __future__ import annotations

import httpx
import pytest
import respx

from plugin.slack.client import SlackApiError, SlackClient

API = "https://slack.com/api"


@pytest.fixture
def client() -> SlackClient:
    return SlackClient("xoxb-tok-123", base_url=API)


class TestPostMessage:
    @respx.mock
    async def test_post_message_posts_and_returns_json(self, client):
        route = respx.post(f"{API}/chat.postMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "channel": "C1", "ts": "1700000000.000100"}
            )
        )
        data = await client.post_message("C1", "hello")
        assert data["ts"] == "1700000000.000100"
        assert route.called
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer xoxb-tok-123"
        assert b'"channel"' in sent.content and b'"C1"' in sent.content
        assert b"hello" in sent.content

    @respx.mock
    async def test_post_message_with_thread_ts(self, client):
        route = respx.post(f"{API}/chat.postMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "channel": "C1", "ts": "1700000000.000200"}
            )
        )
        await client.post_message("C1", "reply", thread_ts="1700000000.000000")
        assert b'"thread_ts"' in route.calls.last.request.content

    @respx.mock
    async def test_post_message_ok_false_raises(self, client):
        # Slack signals logical failure with HTTP 200 + ok:false.
        respx.post(f"{API}/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
        )
        with pytest.raises(SlackApiError, match="channel_not_found"):
            await client.post_message("bad", "hello")

    @respx.mock
    async def test_post_message_raises_on_http_error(self, client):
        respx.post(f"{API}/chat.postMessage").mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(httpx.HTTPStatusError):
            await client.post_message("C1", "hello")


class TestBlocks:
    @respx.mock
    async def test_post_message_includes_blocks_only_when_set(self, client):
        route = respx.post(f"{API}/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.1"})
        )
        await client.post_message("C1", "no blocks here")
        assert b'"blocks"' not in route.calls.last.request.content
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
        await client.post_message("C1", "fallback", blocks=blocks)
        sent = route.calls.last.request.content
        assert b'"blocks"' in sent
        assert b"fallback" in sent  # text fallback still sent alongside blocks

    @respx.mock
    async def test_update_message_includes_blocks_only_when_set(self, client):
        route = respx.post(f"{API}/chat.update").mock(
            return_value=httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.1"})
        )
        await client.update_message("C1", "1.1", "plain")
        assert b'"blocks"' not in route.calls.last.request.content
        await client.update_message("C1", "1.1", "done", blocks=[{"type": "context"}])
        assert b'"blocks"' in route.calls.last.request.content

    @respx.mock
    async def test_respond_posts_ephemeral_to_response_url(self, client):
        url = "https://hooks.slack.com/actions/T1/1/x"
        route = respx.post(url).mock(return_value=httpx.Response(200, text="ok"))
        await client.respond(url, "권한이 없어요.")
        assert route.called
        import json as _json

        body = _json.loads(route.calls.last.request.content)
        assert body["response_type"] == "ephemeral"
        assert body["replace_original"] is False
        assert body["text"] == "권한이 없어요."


class TestUpdateMessage:
    @respx.mock
    async def test_update_message(self, client):
        route = respx.post(f"{API}/chat.update").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "channel": "C1", "ts": "1700000000.000100"}
            )
        )
        data = await client.update_message("C1", "1700000000.000100", "edited")
        assert data["ts"] == "1700000000.000100"
        assert b"edited" in route.calls.last.request.content

    @respx.mock
    async def test_update_message_ok_false_raises(self, client):
        respx.post(f"{API}/chat.update").mock(
            return_value=httpx.Response(200, json={"ok": False, "error": "message_not_found"})
        )
        with pytest.raises(SlackApiError, match="message_not_found"):
            await client.update_message("C1", "ts", "edited")


class TestDeleteMessage:
    @respx.mock
    async def test_delete_message_returns_error_code(self, client):
        respx.post(f"{API}/chat.delete").mock(return_value=httpx.Response(200, json={"ok": True}))
        error = await client.delete_message("C1", "1700000000.000100")
        assert error is None

    @respx.mock
    async def test_delete_message_already_gone_returns_error_not_raised(self, client):
        # An already-deleted message is a no-op: surface the error code so the
        # compensate handler can treat it as success rather than raising.
        respx.post(f"{API}/chat.delete").mock(
            return_value=httpx.Response(200, json={"ok": False, "error": "message_not_found"})
        )
        error = await client.delete_message("C1", "1700000000.000100")
        assert error == "message_not_found"

    @respx.mock
    async def test_delete_message_other_error_raises(self, client):
        respx.post(f"{API}/chat.delete").mock(
            return_value=httpx.Response(200, json={"ok": False, "error": "not_authed"})
        )
        with pytest.raises(SlackApiError, match="not_authed"):
            await client.delete_message("C1", "ts")


class TestInjectedClient:
    @respx.mock
    async def test_uses_injected_async_client(self):
        respx.post(f"{API}/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.1"})
        )
        async with httpx.AsyncClient() as injected:
            sc = SlackClient("xoxb-tok", base_url=API, client=injected)
            data = await sc.post_message("C1", "hi")
        assert data["ts"] == "1.1"
