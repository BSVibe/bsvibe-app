"""Tests for :class:`bsvibe_core.http.HttpClientBase`.

The base client is the shared HTTP foundation for all BSVibe outbound
calls (audit relay, central dispatch, …). It must:

* build httpx.AsyncClient lazily and own it iff caller didn't pass one
* inject ``Authorization`` / ``X-Service-Token`` headers when configured
* retry on ``httpx.HTTPError`` (network) and 5xx responses
* log every request via structlog without ever leaking token values
* expose ``clone(headers=...)`` for per-call header overlay
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
import structlog

from backend.shared.core.http import HttpClientBase, redact_headers


@pytest.fixture
def captured_logs() -> Iterator[list[dict]]:
    """Capture structlog event_dicts emitted during the test."""

    with structlog.testing.capture_logs() as logs:
        yield logs


def _client_with_transport(transport: httpx.MockTransport, **kwargs) -> HttpClientBase:
    http = httpx.AsyncClient(
        transport=transport, base_url=kwargs.pop("base_url", "https://api.test")
    )
    return HttpClientBase("https://api.test", http=http, **kwargs)


class TestRedactHeaders:
    def test_authorization_redacted(self) -> None:
        out = redact_headers({"Authorization": "Bearer secret", "X-Other": "ok"})
        assert out["Authorization"] == "<redacted>"
        assert out["X-Other"] == "ok"

    def test_service_token_redacted(self) -> None:
        out = redact_headers({"X-Service-Token": "abc.def.ghi"})
        assert out["X-Service-Token"] == "<redacted>"

    def test_case_insensitive(self) -> None:
        out = redact_headers({"authorization": "Bearer s", "x-service-token": "t"})
        assert out["authorization"] == "<redacted>"
        assert out["x-service-token"] == "<redacted>"

    def test_no_mutation_of_input(self) -> None:
        original = {"Authorization": "Bearer secret"}
        redact_headers(original)
        assert original["Authorization"] == "Bearer secret"


class TestRequestSuccess:
    async def test_get_returns_2xx(self, captured_logs: list[dict]) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        client = _client_with_transport(httpx.MockTransport(handler))
        try:
            resp = await client.get("/ping")
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
        finally:
            await client.aclose()

        events = [e for e in captured_logs if e.get("event") == "http_request"]
        assert events, "expected http_request log event"
        assert events[-1]["status"] == 200
        assert events[-1]["method"] == "GET"
        assert events[-1]["path"] == "/ping"
        assert "duration_ms" in events[-1]

    async def test_post_with_json_body(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": 1})

        client = _client_with_transport(httpx.MockTransport(handler))
        try:
            resp = await client.post("/items", json={"name": "x"})
            assert resp.status_code == 201
            assert captured["body"] == {"name": "x"}
        finally:
            await client.aclose()


class TestHeaderInjection:
    async def test_default_headers_applied(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            captured["svc"] = request.headers.get("x-service-token")
            return httpx.Response(200, json={})

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.test"
        )
        client = HttpClientBase(
            "https://api.test",
            http=http,
            headers={"Authorization": "Bearer abc", "X-Service-Token": "svc-123"},
        )
        try:
            await client.get("/x")
            assert captured["auth"] == "Bearer abc"
            assert captured["svc"] == "svc-123"
        finally:
            await client.aclose()

    async def test_per_call_headers_overlay(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["x"] = request.headers.get("x-tenant")
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={})

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.test"
        )
        client = HttpClientBase(
            "https://api.test", http=http, headers={"Authorization": "Bearer abc"}
        )
        try:
            await client.request("GET", "/x", headers={"X-Tenant": "t1"})
            assert captured["x"] == "t1"
            assert captured["auth"] == "Bearer abc"
        finally:
            await client.aclose()

    async def test_clone_layers_headers(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={})

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.test"
        )
        base = HttpClientBase("https://api.test", http=http, headers={"X-Common": "c"})
        clone = base.clone(headers={"Authorization": "Bearer t"})
        try:
            await clone.get("/x")
            assert captured["auth"] == "Bearer t"
            # Clone never owns the shared client — its aclose is a no-op
            await clone.aclose()
            assert http.is_closed is False
        finally:
            await http.aclose()


class TestRetryPolicy:
    async def test_retries_on_5xx_then_succeeds(self, captured_logs: list[dict]) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(503, json={"err": "down"})
            return httpx.Response(200, json={"ok": True})

        client = _client_with_transport(httpx.MockTransport(handler), retries=2)
        try:
            resp = await client.get("/x")
            assert resp.status_code == 200
            assert attempts["n"] == 3
        finally:
            await client.aclose()

        retry_events = [e for e in captured_logs if e.get("event") == "http_request_retry"]
        assert len(retry_events) == 2

    async def test_5xx_after_retries_returns_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"err": "down"})

        client = _client_with_transport(httpx.MockTransport(handler), retries=1)
        try:
            resp = await client.get("/x")
            assert resp.status_code == 503
        finally:
            await client.aclose()

    async def test_network_error_retry_exhausted_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        client = _client_with_transport(httpx.MockTransport(handler), retries=2)
        try:
            with pytest.raises(httpx.HTTPError):
                await client.get("/x")
        finally:
            await client.aclose()

    async def test_4xx_not_retried(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(404, json={"err": "missing"})

        client = _client_with_transport(httpx.MockTransport(handler), retries=2)
        try:
            resp = await client.get("/x")
            assert resp.status_code == 404
            assert attempts["n"] == 1
        finally:
            await client.aclose()


class TestLogRedaction:
    async def test_log_record_does_not_contain_bearer_value(
        self, captured_logs: list[dict]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.test"
        )
        client = HttpClientBase(
            "https://api.test",
            http=http,
            headers={
                "Authorization": "Bearer super-secret-jwt",
                "X-Service-Token": "svc-very-secret",
            },
        )
        try:
            await client.get("/x")
        finally:
            await client.aclose()

        rendered = json.dumps(captured_logs)
        assert "super-secret-jwt" not in rendered
        assert "svc-very-secret" not in rendered

    async def test_retry_log_redacted(self, captured_logs: list[dict]) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.test"
        )
        client = HttpClientBase(
            "https://api.test",
            http=http,
            headers={"Authorization": "Bearer leak-me"},
            retries=1,
        )
        try:
            with pytest.raises(httpx.HTTPError):
                await client.get("/x")
        finally:
            await client.aclose()

        rendered = json.dumps(captured_logs)
        assert "leak-me" not in rendered


class TestLifecycle:
    async def test_owns_http_when_caller_did_not_pass_one(self) -> None:
        client = HttpClientBase("https://api.test")
        assert client._owns_http is True
        # Construct underlying client lazily via property access
        http = client.http
        assert isinstance(http, httpx.AsyncClient)
        await client.aclose()
        assert http.is_closed is True

    async def test_does_not_own_caller_supplied_client(self) -> None:
        http = httpx.AsyncClient(base_url="https://api.test")
        client = HttpClientBase("https://api.test", http=http)
        assert client._owns_http is False
        await client.aclose()
        assert http.is_closed is False
        await http.aclose()
