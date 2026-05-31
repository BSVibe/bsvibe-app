"""Tests for ResendClient — the httpx wrapper. httpx is mocked via respx;
no real Resend calls. (rule python-testing: never call real APIs in tests.)"""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.extensions.implementations.email_sender.client import EmailApiError, ResendClient

API = "https://api.resend.com"


@pytest.fixture
def client() -> ResendClient:
    return ResendClient("re_test_key", base_url=API)


class TestSendEmail:
    @respx.mock
    async def test_send_email_posts_and_returns_json(self, client):
        route = respx.post(f"{API}/emails").mock(
            return_value=httpx.Response(200, json={"id": "msg-1"})
        )
        data = await client.send_email(
            sender="BSVibe <no@bsvibe.dev>",
            to="dest@example.com",
            subject="Hi",
            html="<b>hello</b>",
        )
        assert data["id"] == "msg-1"
        assert route.called
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer re_test_key"
        assert sent.headers["Content-Type"] == "application/json"
        assert b"dest@example.com" in sent.content
        assert b"<b>hello</b>" in sent.content
        assert b'"from"' in sent.content

    @respx.mock
    async def test_send_email_text_body(self, client):
        route = respx.post(f"{API}/emails").mock(
            return_value=httpx.Response(200, json={"id": "msg-2"})
        )
        await client.send_email(sender="s@x.dev", to="d@x.dev", subject="S", text="plain body")
        content = route.calls.last.request.content
        assert b"plain body" in content
        assert b'"html"' not in content

    @respx.mock
    async def test_send_email_defaults_to_empty_text_when_no_body(self, client):
        route = respx.post(f"{API}/emails").mock(
            return_value=httpx.Response(200, json={"id": "msg-3"})
        )
        await client.send_email(sender="s@x.dev", to="d@x.dev", subject="S")
        content = route.calls.last.request.content
        assert b'"text"' in content
        assert b'"html"' not in content

    @respx.mock
    async def test_send_email_to_list(self, client):
        route = respx.post(f"{API}/emails").mock(
            return_value=httpx.Response(200, json={"id": "msg-4"})
        )
        await client.send_email(sender="s@x.dev", to=["a@x.dev", "b@x.dev"], subject="S", text="b")
        content = route.calls.last.request.content
        assert b"a@x.dev" in content and b"b@x.dev" in content

    @respx.mock
    async def test_send_email_raises_email_api_error_on_non_2xx(self, client):
        respx.post(f"{API}/emails").mock(
            return_value=httpx.Response(422, json={"message": "Invalid `to` field"})
        )
        with pytest.raises(EmailApiError) as exc:
            await client.send_email(sender="s@x.dev", to="bad", subject="S", text="b")
        assert exc.value.status == 422
        assert "Invalid" in exc.value.message

    @respx.mock
    async def test_send_email_error_with_error_field(self, client):
        respx.post(f"{API}/emails").mock(
            return_value=httpx.Response(401, json={"error": "Unauthorized"})
        )
        with pytest.raises(EmailApiError) as exc:
            await client.send_email(sender="s@x.dev", to="d@x.dev", subject="S", text="b")
        assert exc.value.message == "Unauthorized"

    @respx.mock
    async def test_send_email_error_non_json_body(self, client):
        respx.post(f"{API}/emails").mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(EmailApiError) as exc:
            await client.send_email(sender="s@x.dev", to="d@x.dev", subject="S", text="b")
        assert exc.value.status == 500
        assert exc.value.message == "unknown_error"


class TestInjectedClient:
    @respx.mock
    async def test_uses_injected_async_client(self):
        respx.post(f"{API}/emails").mock(return_value=httpx.Response(200, json={"id": "m"}))
        async with httpx.AsyncClient() as injected:
            rc = ResendClient("tok", base_url=API, client=injected)
            data = await rc.send_email(sender="s@x.dev", to="d@x.dev", subject="S", text="b")
        assert data["id"] == "m"
