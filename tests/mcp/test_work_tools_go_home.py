"""#929 (B), stage 1 PR 4 — a work-tool call runs on the machine that holds the run.

The MCP work tools execute in whichever backend replica received the call, against that
machine's disk and sandbox. With more than one machine, the replica answering may not hold the
run's worktree: the call would read a tree that is not there, or write into one nobody will
ever ship. So a call for a run whose ``home_machine`` is ANOTHER machine is forwarded there —
over a per-machine Redis stream (``bsvibe:machine:<id>``) that each backend process serves for
its own machine — and the home's answer (or its refusal) comes back as the result.

* home here, no home, or a single box (no ``BSVIBE_MACHINE_ID``): the call runs in place, as
  before;
* a call that arrived by forwarding is never forwarded again;
* a home that does not answer fails the call naming the machine — never a silent local run on
  a tree that is not there;
* a request whose caller already gave up is not run late.

See docs/design/stateless-workers.md §2.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from mcp.types import CallToolRequest, CallToolRequestParams
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.identity.db  # noqa: F401 — table registration
import backend.identity.workspaces_db  # noqa: F401
from backend.data.rls import workspace_session_scope
from backend.mcp import home_forwarding
from backend.mcp.api import McpPrincipal, Tool, ToolError, ToolRegistry, ToolScopeDenied
from backend.mcp.home_forwarding import (
    HomeRouting,
    HomeUnreachable,
    home_stream,
    serve_home_calls,
)
from backend.mcp.principal import reset_request_principal, set_request_principal
from backend.mcp.server import build_server, build_tool_executor
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import db_engine


@pytest.fixture
def redis() -> Any:
    import fakeredis
    import fakeredis.aioredis as fakeredis_aio

    return fakeredis_aio.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def _principal(*, run_id: uuid.UUID | None = None) -> McpPrincipal:
    return McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        client_id="worker-task",
        scopes=frozenset({"work:act", "workspace:read"}),
        jti=uuid.uuid4(),
        run_id=run_id,
    )


@contextlib.asynccontextmanager
async def _serving(redis: Any, machine: str, execute: Any) -> AsyncIterator[None]:
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve_home_calls(redis, machine=machine, execute=execute, stop=stop, block_ms=50)
    )
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


# --- the protocol ------------------------------------------------------------------------------


async def test_a_forwarded_call_runs_at_home_and_returns_its_result(redis) -> None:
    seen: list[tuple[McpPrincipal, str, dict[str, Any]]] = []

    async def _execute(principal: McpPrincipal, name: str, arguments: dict[str, Any]) -> str:
        seen.append((principal, name, arguments))
        return json.dumps({"content": "hello from box-a"})

    principal = _principal(run_id=uuid.uuid4())
    routing = HomeRouting(redis=redis, machine="box-b", timeout_s=10)
    async with _serving(redis, "box-a", _execute):
        text = await routing.forward("box-a", principal, "bsvibe_work_file_read", {"path": "x"})

    assert json.loads(text) == {"content": "hello from box-a"}
    assert seen == [(principal, "bsvibe_work_file_read", {"path": "x"})], "same principal"


async def test_a_refusal_at_home_comes_back_as_the_same_refusal(redis) -> None:
    async def _denied(*_: Any) -> str:
        raise ToolScopeDenied("not your run")

    async def _bad(*_: Any) -> str:
        raise ToolError("path escapes the worktree")

    routing = HomeRouting(redis=redis, machine="box-b", timeout_s=10)
    async with _serving(redis, "box-a", _denied):
        with pytest.raises(ToolScopeDenied, match="not your run"):
            await routing.forward("box-a", _principal(run_id=uuid.uuid4()), "bsvibe_work_x", {})
    async with _serving(redis, "box-a", _bad):
        with pytest.raises(ToolError, match="escapes the worktree") as exc:
            await routing.forward("box-a", _principal(run_id=uuid.uuid4()), "bsvibe_work_x", {})
        assert not isinstance(exc.value, ToolScopeDenied)


async def test_a_crash_at_home_is_reported_not_waited_out(redis) -> None:
    async def _boom(*_: Any) -> str:
        raise RuntimeError("secret internal detail")

    routing = HomeRouting(redis=redis, machine="box-b", timeout_s=30)
    started = time.monotonic()
    async with _serving(redis, "box-a", _boom):
        with pytest.raises(ToolError) as exc:
            await routing.forward("box-a", _principal(run_id=uuid.uuid4()), "bsvibe_work_x", {})

    assert time.monotonic() - started < 10
    assert "secret internal detail" not in str(exc.value), "no implementation detail on the wire"


async def test_a_home_that_never_answers_fails_the_call_naming_it(redis) -> None:
    routing = HomeRouting(redis=redis, machine="box-b", timeout_s=1)

    with pytest.raises(HomeUnreachable, match="box-a"):
        await routing.forward("box-a", _principal(run_id=uuid.uuid4()), "bsvibe_work_x", {})


async def test_a_request_its_caller_gave_up_on_is_not_run_late(redis) -> None:
    ran: list[str] = []

    async def _execute(principal: McpPrincipal, name: str, arguments: dict[str, Any]) -> str:
        ran.append(name)
        return "{}"

    routing = HomeRouting(redis=redis, machine="box-b", timeout_s=1)
    with pytest.raises(HomeUnreachable):
        await routing.forward("box-a", _principal(run_id=uuid.uuid4()), "bsvibe_work_late", {})

    async with _serving(redis, "box-a", _execute):
        await asyncio.sleep(0.5)

    assert ran == []


async def test_each_machine_serves_only_its_own_stream(redis) -> None:
    ran: list[str] = []

    async def _execute(principal: McpPrincipal, name: str, arguments: dict[str, Any]) -> str:
        ran.append(name)
        return "{}"

    routing = HomeRouting(redis=redis, machine="box-c", timeout_s=1)
    async with _serving(redis, "box-b", _execute):
        with pytest.raises(HomeUnreachable):
            await routing.forward("box-a", _principal(run_id=uuid.uuid4()), "bsvibe_work_x", {})

    assert ran == []
    assert home_stream("box-a") != home_stream("box-b")


# --- where the decision is made ----------------------------------------------------------------


class _Args(BaseModel):
    pass


class _Out(BaseModel):
    ran: str


class _FakeRouting:
    """Stands in for :class:`HomeRouting`: this process is ``box-b``."""

    machine = "box-b"

    def __init__(self, home: str | None) -> None:
        self.home = home
        self.located: list[uuid.UUID] = []
        self.forwarded: list[tuple[str, str]] = []

    async def locate(self, session: Any, run_id: uuid.UUID) -> str | None:
        self.located.append(run_id)
        return self.home

    async def forward(
        self, home: str, principal: McpPrincipal, name: str, arguments: dict[str, Any]
    ) -> str:
        self.forwarded.append((home, name))
        return json.dumps({"ran": f"at {home}"})


def _registry() -> ToolRegistry:
    async def _here(args: _Args, ctx: Any) -> _Out:
        return _Out(ran="here")

    reg = ToolRegistry()
    for name in ("bsvibe_work_probe", "bsvibe_products_list"):
        reg.register(
            Tool(
                name=name,
                description="probe",
                input_schema=_Args,
                output_schema=_Out,
                handler=_here,
            )
        )
    return reg


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[Any]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _call(server: Any, principal: McpPrincipal, name: str) -> Any:
    token = set_request_principal(principal)
    try:
        result = await server.request_handlers[CallToolRequest](
            CallToolRequest(
                method="tools/call", params=CallToolRequestParams(name=name, arguments={})
            )
        )
    finally:
        reset_request_principal(token)
    return result.root


def _text(result: Any) -> dict[str, Any]:
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


async def test_a_run_whose_home_is_elsewhere_is_answered_from_there(sf) -> None:
    routing = _FakeRouting(home="box-a")
    server = build_server(session_factory=sf, registry=_registry(), home_routing=routing)

    out = _text(await _call(server, _principal(run_id=uuid.uuid4()), "bsvibe_work_probe"))

    assert out == {"ran": "at box-a"}
    assert routing.forwarded == [("box-a", "bsvibe_work_probe")]


@pytest.mark.parametrize("home", ["box-b", None], ids=["home-here", "no-home"])
async def test_a_run_at_home_here_or_nowhere_runs_in_place(sf, home) -> None:
    routing = _FakeRouting(home=home)
    server = build_server(session_factory=sf, registry=_registry(), home_routing=routing)

    out = _text(await _call(server, _principal(run_id=uuid.uuid4()), "bsvibe_work_probe"))

    assert out == {"ran": "here"} and routing.forwarded == []


async def test_a_single_box_runs_everything_in_place(sf) -> None:
    server = build_server(session_factory=sf, registry=_registry(), home_routing=None)

    out = _text(await _call(server, _principal(run_id=uuid.uuid4()), "bsvibe_work_probe"))

    assert out == {"ran": "here"}


async def test_an_ordinary_token_is_never_forwarded(sf) -> None:
    routing = _FakeRouting(home="box-a")
    server = build_server(session_factory=sf, registry=_registry(), home_routing=routing)

    out = _text(await _call(server, _principal(run_id=None), "bsvibe_products_list"))

    assert out == {"ran": "here"} and routing.located == []


async def test_a_refused_tool_is_refused_before_anything_is_forwarded(sf) -> None:
    routing = _FakeRouting(home="box-a")
    server = build_server(session_factory=sf, registry=_registry(), home_routing=routing)

    result = await _call(server, _principal(run_id=uuid.uuid4()), "bsvibe_products_list")

    assert result.isError and routing.forwarded == []


async def test_a_forwarded_call_is_never_forwarded_again(sf) -> None:
    routing = _FakeRouting(home="box-a")
    execute = build_tool_executor(session_factory=sf, registry=_registry(), home_routing=routing)

    text = await execute(_principal(run_id=uuid.uuid4()), "bsvibe_work_probe", {}, forwarded=True)

    assert json.loads(text) == {"ran": "here"} and routing.located == []


async def test_the_home_is_read_from_the_run(sf) -> None:
    ws, rid = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(
            ExecutionRun(
                id=rid,
                workspace_id=ws,
                status=RunStatus.RUNNING,
                payload={},
                home_machine="box-a",
                created_at=now,
                updated_at=now,
            )
        )
        await s.commit()
    routing = HomeRouting(redis=None, machine="box-b", timeout_s=1)

    async with sf() as s, workspace_session_scope(s, ws):
        assert await routing.locate(s, rid) == "box-a"
        assert await routing.locate(s, uuid.uuid4()) is None


async def test_a_call_on_one_machine_is_answered_by_the_other(sf, redis) -> None:
    """End to end, only Redis faked: box-b receives the call, box-a (the run's home) runs it."""
    import functools

    ws, rid = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(
            ExecutionRun(
                id=rid,
                workspace_id=ws,
                status=RunStatus.RUNNING,
                payload={},
                home_machine="box-a",
                created_at=now,
                updated_at=now,
            )
        )
        await s.commit()
    ran_on: list[str] = []

    def _registry_on(machine: str) -> ToolRegistry:
        async def _here(args: _Args, ctx: Any) -> _Out:
            ran_on.append(machine)
            return _Out(ran=machine)

        reg = ToolRegistry()
        reg.register(
            Tool(
                name="bsvibe_work_probe",
                description="probe",
                input_schema=_Args,
                output_schema=_Out,
                handler=_here,
            )
        )
        return reg

    a = HomeRouting(redis=redis, machine="box-a", timeout_s=10)
    b = HomeRouting(redis=redis, machine="box-b", timeout_s=10)
    on_a = build_tool_executor(session_factory=sf, registry=_registry_on("box-a"), home_routing=a)
    on_b = build_tool_executor(session_factory=sf, registry=_registry_on("box-b"), home_routing=b)
    principal = McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=ws,
        client_id="worker-task",
        scopes=frozenset(),
        jti=uuid.uuid4(),
        run_id=rid,
    )

    async with _serving(redis, "box-a", functools.partial(on_a, forwarded=True)):
        text = await on_b(principal, "bsvibe_work_probe", {})

    assert json.loads(text) == {"ran": "box-a"} and ran_on == ["box-a"]


# --- the wiring --------------------------------------------------------------------------------


def test_the_routing_exists_only_where_a_machine_is_named(redis) -> None:
    from backend.config import get_settings

    s = get_settings().model_copy(update={"machine_id": "box-a", "shell_exec_timeout_max_s": 60.0})
    routing = home_forwarding.build_home_routing(s, redis)
    assert routing is not None and routing.machine == "box-a"
    assert routing.timeout_s > 60.0, "the wait outlasts the longest command a tool may run"

    assert (
        home_forwarding.build_home_routing(s.model_copy(update={"machine_id": ""}), redis) is None
    )
    assert home_forwarding.build_home_routing(s, None) is None


@pytest.mark.parametrize("machine", ["box-a", ""], ids=["named", "single-box"])
async def test_the_api_serves_its_machine_and_routes_through_it(
    sf, redis, monkeypatch, machine
) -> None:
    """The real lifespan: it hands the server the routing, and serves this machine's stream
    with calls that never bounce on."""
    import functools

    from fastapi import FastAPI

    from backend.config import get_settings
    from backend.mcp import lifespan

    monkeypatch.setattr(get_settings(), "machine_id", machine)
    monkeypatch.setattr(lifespan, "get_dispatch_redis_client", lambda _s: redis)
    served: list[dict[str, Any]] = []
    built: list[Any] = []
    real_build_server = lifespan.build_server

    async def _serve(redis_: Any, **kw: Any) -> None:
        served.append(kw)
        await kw["stop"].wait()

    def _build_server(**kw: Any) -> Any:
        built.append(kw.get("home_routing"))
        return real_build_server(**kw)

    monkeypatch.setattr(lifespan, "serve_home_calls", _serve)
    monkeypatch.setattr(lifespan, "build_server", _build_server)

    async with lifespan.mcp_lifespan(FastAPI(), session_factory=sf):
        await asyncio.sleep(0)

    if not machine:
        assert built == [None] and served == []
        return
    assert built[0] is not None and built[0].machine == "box-a"
    assert [kw["machine"] for kw in served] == ["box-a"], "nobody would answer this stream"
    execute = served[0]["execute"]
    assert isinstance(execute, functools.partial) and execute.keywords == {"forwarded": True}, (
        "a call that arrived by forwarding must not bounce again"
    )
    assert served[0]["stop"].is_set(), "stopped with the app"
