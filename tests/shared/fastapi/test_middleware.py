"""Tests for RequestIdMiddleware.

Contract:

* If the incoming request has an ``x-request-id`` header, it is used as
  the request id (any non-empty value).
* Otherwise a fresh UUID4 hex string is generated.
* The request id is stored on ``request.state.request_id`` for downstream
  handlers, AND echoed back as the response ``x-request-id`` header so
  callers can correlate without a tracing system.
* For the duration of the request the id is bound to the structlog
  ``contextvars`` under the key ``request_id`` so every ``logger.info``
  inside the handler emits ``request_id=<value>`` automatically.
"""

from __future__ import annotations

import io
import json
import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.shared.core import configure_logging
from backend.shared.fastapi.middleware import RequestIdMiddleware


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo")
    async def echo(request: Request) -> dict[str, str]:
        return {"request_id": request.state.request_id}

    return app


class TestRequestIdMiddleware:
    def test_generates_uuid_when_header_missing(self) -> None:
        client = TestClient(_make_app())
        resp = client.get("/echo")
        assert resp.status_code == 200
        body = resp.json()
        # Must look like a hex UUID — otherwise we are leaking a default
        # placeholder that downstream log scrapers cannot correlate.
        request_id = body["request_id"]
        uuid.UUID(request_id)
        assert resp.headers["x-request-id"] == request_id

    def test_propagates_incoming_header(self) -> None:
        client = TestClient(_make_app())
        resp = client.get("/echo", headers={"X-Request-Id": "abc-123"})
        assert resp.status_code == 200
        assert resp.json()["request_id"] == "abc-123"
        assert resp.headers["x-request-id"] == "abc-123"

    def test_empty_incoming_header_replaced_with_uuid(self) -> None:
        # Empty header should NOT poison logs — generate a fresh id.
        client = TestClient(_make_app())
        resp = client.get("/echo", headers={"X-Request-Id": ""})
        assert resp.status_code == 200
        request_id = resp.json()["request_id"]
        uuid.UUID(request_id)

    def test_each_request_gets_unique_id(self) -> None:
        client = TestClient(_make_app())
        a = client.get("/echo").json()["request_id"]
        b = client.get("/echo").json()["request_id"]
        assert a != b


class TestStructlogBinding:
    def test_logger_inside_handler_includes_request_id(self) -> None:
        stream = io.StringIO()
        configure_logging(level="info", json_output=True, stream=stream)

        captured: dict[str, str] = {}
        log = structlog.get_logger("test")

        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        @app.get("/log")
        async def emit() -> dict[str, str]:
            log.info("inside_handler")
            return {"ok": "yes"}

        client = TestClient(app)
        resp = client.get("/log", headers={"X-Request-Id": "rid-42"})
        assert resp.status_code == 200

        # Find the JSON line emitted by the handler.
        for line in stream.getvalue().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "inside_handler":
                captured = rec
                break

        assert captured.get("request_id") == "rid-42"

    def test_request_id_is_unbound_after_request(self) -> None:
        # contextvars must not leak across requests — otherwise a request
        # without an X-Request-Id header would inherit the previous one.
        stream = io.StringIO()
        configure_logging(level="info", json_output=True, stream=stream)
        log = structlog.get_logger("test")

        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        @app.get("/noop")
        async def noop() -> dict[str, str]:
            return {"ok": "yes"}

        client = TestClient(app)
        client.get("/noop", headers={"X-Request-Id": "should-not-leak"})

        # Now log from outside any request — the previous request_id must
        # not be present.
        stream.truncate(0)
        stream.seek(0)
        log.info("outside_request")
        for line in stream.getvalue().splitlines():
            rec = json.loads(line)
            if rec.get("event") == "outside_request":
                assert "request_id" not in rec
                return
        raise AssertionError("did not capture log line")
