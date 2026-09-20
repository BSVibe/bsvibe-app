"""opencode acts through BSVibe's tools, or it does not act at all (#1000).

``claude_code`` has carried this contract since #553: the CLI is handed BSVibe's tools
over MCP with a run-scoped token, and its own native tools are taken away. opencode was
refused until its shape was verified against the real binary — guessing a CLI's contract
is how wrappers rot. It is verified now (opencode 1.17.3, request-body capture, 2026-09-21),
and these tests pin what the measurement found.

Three findings drive the shape below, and each one has a test here because each one fails
SILENTLY — no error, no red, just a model with the wrong hands:

1. **The MCP server is registered at RUNTIME** (``POST /mcp?directory=``), not written into
   the workspace's ``opencode.json``. The serve daemon caches a directory's MCP config for
   its whole lifetime: a second run in the same directory keeps using the FIRST run's
   ``Authorization`` header — measured, and even an explicit disconnect+connect did not
   refresh it. The worker's ``server_sandbox`` directory is ONE fixed path shared by every
   task and every tenant (``main.py``), so the config-file route would hand run B the token
   scoped to run A's run. ``POST /mcp`` overrides the cache and never puts the token on disk.

2. **The server name is unique per task**, because that shared directory is also shared by
   CONCURRENT tasks. One registration named ``bsvibe`` would be overwritten by whichever
   task registered last, and every session in the directory would then be offered the other
   task's surface. A per-task name keeps each run's allowlist naming its own tools only.

3. **``"*": false`` must be the FIRST key of the ``tools`` map.** Later keys override
   earlier ones, so ``{"read": true, "*": false}`` yields ZERO tools — with no error, and a
   model that answers as if it simply had nothing to work with. Python dicts preserve
   insertion order and that order reaches opencode verbatim, so the ORDER is the contract:
   asserting only the map's contents would stay green through exactly this bug.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.executors.worker import opencode_server
from backend.executors.worker.opencode import OpenCodeExecutor
from tests.executors.worker._drain import drain

pytestmark = pytest.mark.asyncio


_MCP_CONFIG = json.dumps(
    {
        "mcpServers": {
            "bsvibe": {
                "type": "http",
                "url": "https://api.bsvibe.dev/mcp/",
                "headers": {"Authorization": "Bearer run-scoped-token"},
            }
        }
    }
)
_ALLOWED = ["mcp__bsvibe__bsvibe_work_file_read", "mcp__bsvibe__bsvibe_work_shell_exec"]
_TASK_ID = "3f2a1b4c-5d6e-7f80-9a1b-2c3d4e5f6071"


class _FakeServe:
    """``opencode serve``'s HTTP surface, in the shapes the live daemon returned."""

    def __init__(self, *, connect_status: str = "connected") -> None:
        self.connect_status = connect_status
        self.mcp_adds: list[tuple[str, dict[str, Any]]] = []
        self.mcp_disconnects: list[str] = []
        self.message_requests: list[dict[str, Any]] = []
        self.session_urls: list[str] = []

    def transport(self) -> httpx.MockTransport:
        async def _handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST" and path == "/mcp":
                body = json.loads(request.content.decode("utf-8"))
                self.mcp_adds.append((str(request.url), body))
                return httpx.Response(200, json={body["name"]: {"status": self.connect_status}})
            if (
                request.method == "POST"
                and path.startswith("/mcp/")
                and path.endswith("/disconnect")
            ):
                self.mcp_disconnects.append(str(request.url))
                return httpx.Response(200, json=True)
            if request.method == "POST" and path == "/session":
                self.session_urls.append(str(request.url))
                return httpx.Response(200, json={"id": "sid-1"})
            if request.method == "POST" and path.endswith("/message"):
                self.message_requests.append(json.loads(request.content.decode("utf-8")))
                return httpx.Response(200, json={"parts": [{"type": "text", "text": "ok"}]})
            if request.method == "POST" and path.endswith("/abort"):
                return httpx.Response(200, json={})
            return httpx.Response(404)

        return httpx.MockTransport(_handler)


@pytest.fixture(autouse=True)
def _serve_url() -> Any:
    opencode_server.set_serve_url("http://127.0.0.1:4096")
    yield
    opencode_server.clear_serve_url()


def _ctx(workspace: str, **over: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "task_id": _TASK_ID,
        "workspace_dir": workspace,
        "agentic": True,
        "mcp_config": _MCP_CONFIG,
        "allowed_tools": list(_ALLOWED),
    }
    ctx.update(over)
    return ctx


def _query(url: str, key: str) -> str | None:
    from urllib.parse import parse_qs, urlparse

    got = parse_qs(urlparse(url).query).get(key)
    return got[0] if got else None


# ── the run's MCP surface is registered at runtime, scoped to its directory ──


async def test_an_agentic_run_registers_the_dispatched_mcp_server_at_runtime(
    tmp_path: Path,
) -> None:
    serve = _FakeServe()
    executor = OpenCodeExecutor(http_transport=serve.transport())

    await drain(executor.execute("build it", _ctx(str(tmp_path))))

    assert len(serve.mcp_adds) == 1, "the run's MCP server must be registered exactly once"
    url, body = serve.mcp_adds[0]
    # Same directory as the session: opencode scopes an MCP registration to the directory,
    # so a mismatch would register the surface where the session cannot see it.
    assert _query(url, "directory") == str(tmp_path)
    assert body["config"]["type"] == "remote"
    assert body["config"]["url"] == "https://api.bsvibe.dev/mcp/"
    assert body["config"]["enabled"] is True
    # The run-scoped bearer arrives as a header — the same token the dispatch minted.
    assert body["config"]["headers"]["Authorization"] == "Bearer run-scoped-token"


async def test_the_run_token_never_lands_on_disk_in_the_workspace(tmp_path: Path) -> None:
    """The directory is shared by every task on this worker (and every tenant).

    A token written into ``<workspace>/opencode.json`` would outlive the run, be readable
    by the next tenant's task, and — because the daemon caches a directory's config for its
    lifetime — be the token that task's MCP client actually uses.
    """
    serve = _FakeServe()
    executor = OpenCodeExecutor(http_transport=serve.transport())

    await drain(executor.execute("build it", _ctx(str(tmp_path))))

    # The proposition is about the TOKEN, not about tidiness: the worker's test fixtures
    # put their own dirs in here. Read every file that exists after the run and assert the
    # bearer is in none of them.
    holding = [
        str(p)
        for p in tmp_path.rglob("*")
        if p.is_file() and "run-scoped-token" in p.read_text(errors="ignore")
    ]
    assert holding == [], f"the run token was written to disk in the workspace: {holding}"
    assert not (tmp_path / "opencode.json").exists(), (
        "an opencode.json in the workspace is cached by the daemon for its whole lifetime — "
        "the next task in this shared directory would keep using THIS run's token"
    )


async def test_each_task_registers_under_its_own_server_name(tmp_path: Path) -> None:
    """Concurrent tasks share the directory — one fixed name would collide."""
    serve = _FakeServe()
    executor = OpenCodeExecutor(http_transport=serve.transport())

    await drain(executor.execute("a", _ctx(str(tmp_path))))
    await drain(
        executor.execute("b", _ctx(str(tmp_path), task_id="99887766-5544-3322-1100-aabbccddeeff"))
    )

    names = [body["name"] for _url, body in serve.mcp_adds]
    assert len(set(names)) == 2, f"two tasks must not share one MCP server name: {names}"
    assert all(n.startswith("bsvibe") for n in names), names
    # opencode prefixes every MCP tool with the server name; the provider caps a tool name at
    # 64 chars, and our longest work tool is 32. Keep the prefix short enough to fit.
    assert all(len(n) <= 31 for n in names), names


# ── the model is offered our tools and nothing else ─────────────────────────


async def test_the_tools_map_disables_everything_first_then_allows_ours(
    tmp_path: Path,
) -> None:
    """ORDER is the contract: a later key overrides an earlier one.

    ``{"bsvibe…_read": true, "*": false}`` yields zero tools and no error — the model then
    answers as if it had no tools, which is indistinguishable from a chat turn.
    """
    serve = _FakeServe()
    executor = OpenCodeExecutor(http_transport=serve.transport())

    await drain(executor.execute("build it", _ctx(str(tmp_path))))

    tools = serve.message_requests[0]["tools"]
    keys = list(tools)
    assert keys[0] == "*", f"the wildcard must come FIRST or it overrides our names: {keys}"
    assert tools["*"] is False
    assert all(tools[k] is True for k in keys[1:]), tools


async def test_our_tool_names_carry_the_runs_server_prefix(tmp_path: Path) -> None:
    """opencode surfaces an MCP tool as ``<server>_<tool>`` — measured for remote MCP too.

    The dispatch sends claude's ``mcp__bsvibe__<tool>`` spelling; naming opencode's tools
    with it would allow a set that does not exist, leaving the model with zero tools.
    """
    serve = _FakeServe()
    executor = OpenCodeExecutor(http_transport=serve.transport())

    await drain(executor.execute("build it", _ctx(str(tmp_path))))

    server = serve.mcp_adds[0][1]["name"]
    tools = serve.message_requests[0]["tools"]
    assert set(tools) - {"*"} == {
        f"{server}_bsvibe_work_file_read",
        f"{server}_bsvibe_work_shell_exec",
    }


# ── absence is refused, never downgraded ────────────────────────────────────


async def test_a_failed_registration_aborts_instead_of_running_toolless(
    tmp_path: Path,
) -> None:
    """If the MCP server did not connect, ``{"*": false}`` leaves the model with NOTHING.

    An agent with no tools does not report that it has none: it fabricates (the claude_code
    guard exists because one did exactly that and the run was recorded as a success).
    """
    serve = _FakeServe(connect_status="failed")
    executor = OpenCodeExecutor(http_transport=serve.transport())

    result = await drain(executor.execute("build it", _ctx(str(tmp_path))))

    assert result.success is False
    assert result.error_message and "MCP" in result.error_message
    assert serve.message_requests == [], "a toolless agent run must never be sent"


async def test_an_agentic_task_without_an_mcp_surface_is_refused(tmp_path: Path) -> None:
    """There is no third executor shape: no local agent run to fall back to."""
    serve = _FakeServe()
    executor = OpenCodeExecutor(http_transport=serve.transport())

    result = await drain(
        executor.execute("build it", _ctx(str(tmp_path), mcp_config="", allowed_tools=[]))
    )

    assert result.success is False
    assert result.error_message
    assert serve.message_requests == []
    assert serve.mcp_adds == []


# ── cleanup, and the chat turn is untouched ─────────────────────────────────


async def test_the_runs_server_is_disconnected_when_the_task_ends(tmp_path: Path) -> None:
    """A registration left behind holds a live connection with a spent run token."""
    serve = _FakeServe()
    executor = OpenCodeExecutor(http_transport=serve.transport())

    await drain(executor.execute("build it", _ctx(str(tmp_path))))

    server = serve.mcp_adds[0][1]["name"]
    assert len(serve.mcp_disconnects) == 1
    assert f"/mcp/{server}/disconnect" in serve.mcp_disconnects[0]
    assert _query(serve.mcp_disconnects[0], "directory") == str(tmp_path)


async def test_a_chat_turn_registers_nothing_and_keeps_tools_off(tmp_path: Path) -> None:
    serve = _FakeServe()
    executor = OpenCodeExecutor(http_transport=serve.transport())

    await drain(
        executor.execute(
            "what is 6*7?",
            _ctx(str(tmp_path), agentic=False, mcp_config="", allowed_tools=[]),
        )
    )

    assert serve.mcp_adds == []
    assert serve.message_requests[0]["tools"] == {"*": False}


async def test_a_respawned_daemon_is_registered_again_before_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The surface lives in the daemon's memory, not on disk.

    When the daemon dies mid-run the executor re-spawns it and retries once. A fresh
    process holds none of our registrations, so a retry that skipped re-registering would
    send the SAME tools map against a daemon that has never heard of those names — zero
    tools, no error, a fabricating agent.
    """
    calls = {"session": 0}
    serve = _FakeServe()
    inner = serve.transport()

    async def _flaky(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/session":
            calls["session"] += 1
            if calls["session"] == 1:
                raise httpx.ConnectError("connection refused", request=request)
        return await inner.handle_async_request(request)

    async def _ensure(_settings: Any) -> str:
        return "http://127.0.0.1:4096"

    monkeypatch.setattr(opencode_server, "ensure_serve_running", _ensure)
    executor = OpenCodeExecutor(http_transport=httpx.MockTransport(_flaky))

    result = await drain(executor.execute("build it", _ctx(str(tmp_path))))

    assert result.success is True
    assert len(serve.mcp_adds) == 2, (
        f"the re-spawned daemon must be registered again, got {len(serve.mcp_adds)} POST /mcp"
    )
    assert len(serve.message_requests) == 1
