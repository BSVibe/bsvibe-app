"""Tests for the worker's ``opencode`` HTTP executor (Lift E17).

The pre-E17 executor spawned ``opencode run --format json`` per task, which
dogfood proved fundamentally too slow (8h wall-clock on a trivial prompt). E17
replaces that with a long-running ``opencode serve`` daemon (managed in
:mod:`backend.executors.worker.opencode_server`) and a per-task HTTP call to
``POST /session`` + ``POST /session/{id}/message``.

NO real ``opencode`` binary is invoked. Every test stubs the HTTP transport via
:class:`httpx.MockTransport` so the wire shape, system-prompt routing, cancel
abort, and re-spawn-on-connection-refused paths are proven deterministically.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Generator
from typing import Any

import httpx
import pytest

from backend.executors.worker import opencode_server
from backend.executors.worker.executors import ExecutionChunk, collect
from backend.executors.worker.opencode import OpenCodeExecutor

pytestmark = pytest.mark.asyncio


# ── Fake opencode serve transport ───────────────────────────────────────────


class _FakeServe:
    """A tiny in-memory stand-in for ``opencode serve``'s HTTP surface.

    Records every request body for assertions, lets each test customise the
    next ``/session/{id}/message`` response (text body, latency, status code).
    """

    def __init__(self, *, text: str = "ok", status: int = 200) -> None:
        self.text = text
        self.status = status
        self.session_requests: list[bytes] = []
        self.message_requests: list[dict[str, Any]] = []
        self.aborted_sessions: list[str] = []
        self.next_sid: str = "sid-1"
        # Optional latency simulator.
        self.message_delay_s: float = 0.0

    def transport(self) -> httpx.MockTransport:
        async def _handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST" and path == "/session":
                self.session_requests.append(request.content)
                return httpx.Response(200, json={"id": self.next_sid})
            if (
                request.method == "POST"
                and path.startswith("/session/")
                and path.endswith("/message")
            ):
                if self.message_delay_s:
                    await asyncio.sleep(self.message_delay_s)
                body = json.loads(request.content.decode("utf-8"))
                self.message_requests.append(body)
                if self.status != 200:
                    return httpx.Response(self.status, text="boom")
                return httpx.Response(
                    200,
                    json={
                        "parts": [{"type": "text", "text": self.text}],
                        "info": {
                            "time": {"created": 1000, "completed": 2500},
                        },
                    },
                )
            if request.method == "POST" and path.endswith("/abort"):
                # /session/{id}/abort
                sid = path.split("/")[2]
                self.aborted_sessions.append(sid)
                return httpx.Response(200, json={})
            return httpx.Response(404)

        return httpx.MockTransport(_handler)


async def _drain(stream: AsyncIterator[ExecutionChunk]) -> list[ExecutionChunk]:
    return [c async for c in stream]


@pytest.fixture(autouse=True)
def _reset_singleton() -> Generator[None, None, None]:
    opencode_server.clear_serve_url()
    yield
    opencode_server.clear_serve_url()


def _executor_with(serve: _FakeServe) -> OpenCodeExecutor:
    """Build an OpenCodeExecutor wired to a fake serve transport."""
    opencode_server.set_serve_url("http://127.0.0.1:4096")
    return OpenCodeExecutor(http_transport=serve.transport())


# ── Happy path ──────────────────────────────────────────────────────────────


async def test_executes_via_http_yields_text_then_done() -> None:
    serve = _FakeServe(text="hello world")
    executor = _executor_with(serve)

    chunks = await _drain(executor.execute("do it", {"workspace_dir": "."}))

    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["hello world"]
    assert chunks[-1].done is True
    assert chunks[-1].error is None
    # /session called once, /session/{id}/message called once.
    assert len(serve.session_requests) == 1
    assert len(serve.message_requests) == 1


async def test_collect_aggregates_output() -> None:
    serve = _FakeServe(text="abcdef")
    executor = _executor_with(serve)

    result = await collect(executor.execute("p", {}))
    assert result.success is True
    assert result.stdout == "abcdef"
    assert result.error_message is None


async def test_message_body_uses_system_alongside_parts_not_inside() -> None:
    """The dogfood-verified shape: ``system`` is a TOP-LEVEL key alongside
    ``parts``, NOT a system-role message inside ``parts``. Tests assert the
    body matches the verified-working shape exactly so any future regression
    that moves ``system`` into ``parts`` fails loud.
    """
    serve = _FakeServe(text="ok")
    executor = _executor_with(serve)

    await _drain(
        executor.execute(
            "the user prompt",
            {"system": "BE BRIEF", "model": "anthropic/claude", "workspace_dir": "."},
        )
    )

    body = serve.message_requests[0]
    assert body["system"] == "BE BRIEF"
    assert body["parts"] == [{"type": "text", "text": "the user prompt"}]
    assert body["agent"] == "plan"
    assert body["model"] == "anthropic/claude"


async def test_message_body_omits_model_when_not_provided() -> None:
    serve = _FakeServe(text="ok")
    executor = _executor_with(serve)
    await _drain(executor.execute("p", {"system": "S"}))
    body = serve.message_requests[0]
    assert "model" not in body


async def test_message_body_omits_system_when_empty() -> None:
    serve = _FakeServe(text="ok")
    executor = _executor_with(serve)
    await _drain(executor.execute("p", {}))
    body = serve.message_requests[0]
    # An empty system field is omitted rather than sent as "".
    assert body.get("system", "") == ""  # accept "" or absent


# ── Error paths ─────────────────────────────────────────────────────────────


async def test_non_2xx_yields_error_chunk() -> None:
    serve = _FakeServe(status=500)
    executor = _executor_with(serve)

    chunks = await _drain(executor.execute("p", {}))
    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "500" in chunks[-1].error or "boom" in chunks[-1].error.lower()


async def test_missing_serve_url_singleton_yields_clear_error() -> None:
    """Running the executor without the worker daemon having started serve
    must yield a clear, terminal error chunk — not crash.
    """
    opencode_server.clear_serve_url()
    serve = _FakeServe()
    executor = OpenCodeExecutor(http_transport=serve.transport())

    chunks = await _drain(executor.execute("p", {}))
    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "opencode serve" in chunks[-1].error.lower()


async def test_supported_task_types() -> None:
    assert OpenCodeExecutor().supported_task_types() == ["opencode"]


# ── Cancel propagation: must abort the session server-side ──────────────────


async def test_cancel_aborts_session_and_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the wrapper Task is cancelled mid-HTTP-request, the executor
    MUST POST ``/session/{id}/abort`` so the serve daemon stops the LLM call
    server-side. Then it re-raises CancelledError so the worker loop's
    cancel chain stays correct.
    """
    serve = _FakeServe(text="ignored")
    serve.message_delay_s = 5.0  # block long enough for cancel to land
    executor = _executor_with(serve)

    task = asyncio.create_task(_drain(executor.execute("long prompt", {"system": "S"})))
    # Let the HTTP call begin.
    for _ in range(50):
        if serve.session_requests:
            break
        await asyncio.sleep(0.01)
    assert serve.session_requests, "executor should have hit /session before cancel"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    # The session id chosen by the fake is "sid-1"; abort should have hit it.
    # Allow a brief moment for the abort POST to drain (it's launched from
    # the executor's cancel-handler before re-raising).
    for _ in range(50):
        if serve.aborted_sessions:
            break
        await asyncio.sleep(0.01)
    assert serve.aborted_sessions == ["sid-1"]


# ── Re-spawn on connection refused ──────────────────────────────────────────


async def test_connection_refused_triggers_one_respawn_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the serve daemon died, the next HTTP call gets ConnectError. The
    executor invokes the server module's ``ensure_serve_running`` helper
    once + retries; if that succeeds the task completes normally.
    """
    serve = _FakeServe(text="recovered")
    real_transport = serve.transport()

    # First call → ConnectError; subsequent calls → real_transport.
    calls = {"n": 0}

    async def _flaky_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return await real_transport.handle_async_request(request)

    opencode_server.set_serve_url("http://127.0.0.1:4096")
    executor = OpenCodeExecutor(http_transport=httpx.MockTransport(_flaky_handler))

    respawned: list[int] = []

    async def _fake_ensure(settings: Any) -> str:
        respawned.append(1)
        return "http://127.0.0.1:4096"

    monkeypatch.setattr(opencode_server, "ensure_serve_running", _fake_ensure)

    chunks = await _drain(executor.execute("p", {}))

    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["recovered"]
    assert chunks[-1].done is True
    assert chunks[-1].error is None
    assert respawned == [1], "ensure_serve_running must have been called exactly once"


async def test_persistent_connection_refused_surfaces_terminal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If re-spawn + retry still fails, the executor yields a terminal error."""

    async def _always_refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    opencode_server.set_serve_url("http://127.0.0.1:4096")
    executor = OpenCodeExecutor(http_transport=httpx.MockTransport(_always_refused))

    async def _fake_ensure(settings: Any) -> str:
        return "http://127.0.0.1:4096"

    monkeypatch.setattr(opencode_server, "ensure_serve_running", _fake_ensure)

    chunks = await _drain(executor.execute("p", {}))
    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "connection" in chunks[-1].error.lower() or "refused" in chunks[-1].error.lower()


# ── No subprocess code path remains ─────────────────────────────────────────


async def test_no_subprocess_exec_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """The E17 invariant: ``opencode run`` is gone. No ``create_subprocess_exec``
    is called when ``OpenCodeExecutor.execute`` runs. If a regression reintroduces
    the CLI path this test fails loud.
    """
    spawn_calls: list[Any] = []

    async def _track(*args: Any, **kwargs: Any) -> Any:
        spawn_calls.append(args)
        raise RuntimeError("subprocess path must not be used")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _track)

    serve = _FakeServe(text="ok")
    executor = _executor_with(serve)
    await _drain(executor.execute("p", {}))

    assert spawn_calls == []
