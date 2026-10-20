"""A work-tool call runs on the machine that holds the run (#929, B).

The MCP work tools execute in whichever backend replica received the call, against that
machine's disk and sandbox. With more than one machine, that replica may not hold the run's
worktree. A call for a run whose ``home_machine`` is another machine is therefore forwarded
there — docs/design/stateless-workers.md §2.1:

* **Request** — ``XADD bsvibe:machine:<home>`` with the call (tool, arguments, the principal it
  was authorised as) and a deadline. Each backend process serves its own machine's stream
  (:func:`serve_home_calls`) through a consumer group, so any replica ON that machine — which
  shares its disk — may answer.
* **Reply** — ``LPUSH bsvibe:machine:reply:<request>``, which the caller ``BLPOP``\\ s. A refusal
  at home comes back as the same refusal; a crash comes back as a generic failure (no
  implementation detail on the wire, as in :meth:`ToolRegistry.call_tool`).
* **At most once.** A tool call is not idempotent (a write, a shell command), so a request is
  acknowledged as it is read and never redelivered. One whose caller already gave up is
  dropped, not run late.

No ``BSVIBE_MACHINE_ID`` (a single box) means no routing at all: every call runs in place.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select

from backend.config import Settings
from backend.mcp.api import McpPrincipal, ToolError, ToolScopeDenied
from backend.workflow.infrastructure.db import ExecutionRun

logger = structlog.get_logger(__name__)

HOME_STREAM_PREFIX = "bsvibe:machine:"
_REPLY_PREFIX = "bsvibe:machine:reply:"
_GROUP = "home-calls"
_STREAM_MAXLEN = 1000
_REPLY_TTL_S = 120
#: Beyond the longest command a tool may run: provisioning a sandbox, restoring a tree.
_FORWARD_SLACK_S = 120.0

#: Runs one tool call locally and returns its result as wire text.
LocalExecute = Callable[[McpPrincipal, str, dict[str, Any]], Awaitable[str]]


class HomeUnreachable(ToolError):  # noqa: N818 — reads as the condition, like ToolScopeDenied
    """The machine running this run did not answer in time."""


def home_stream(machine: str) -> str:
    return f"{HOME_STREAM_PREFIX}{machine}"


def _reply_key(request_id: str) -> str:
    return f"{_REPLY_PREFIX}{request_id}"


def _principal_to_wire(p: McpPrincipal) -> dict[str, Any]:
    return {
        "user_id": str(p.user_id),
        "workspace_id": str(p.workspace_id),
        "client_id": p.client_id,
        "scopes": sorted(p.scopes),
        "jti": str(p.jti),
        "run_id": str(p.run_id) if p.run_id is not None else None,
    }


def _principal_from_wire(d: dict[str, Any]) -> McpPrincipal:
    return McpPrincipal(
        user_id=uuid.UUID(d["user_id"]),
        workspace_id=uuid.UUID(d["workspace_id"]),
        client_id=d["client_id"],
        scopes=frozenset(d["scopes"]),
        jti=uuid.UUID(d["jti"]),
        run_id=uuid.UUID(d["run_id"]) if d.get("run_id") else None,
    )


@dataclass(frozen=True)
class HomeRouting:
    """This process's view of where runs live: its own machine, and the way to the others."""

    redis: Any
    machine: str
    timeout_s: float

    async def locate(self, session: Any, run_id: uuid.UUID) -> str | None:
        """The machine driving ``run_id`` right now, or ``None``."""
        home = await session.scalar(
            select(ExecutionRun.home_machine).where(ExecutionRun.id == run_id)
        )
        return str(home) if home else None

    async def forward(
        self, home: str, principal: McpPrincipal, name: str, arguments: dict[str, Any]
    ) -> str:
        """Run the call on ``home`` and return its result text (or raise its refusal)."""
        request_id = uuid.uuid4().hex
        call = {
            "request_id": request_id,
            "name": name,
            "arguments": arguments,
            "principal": _principal_to_wire(principal),
            "deadline": time.time() + self.timeout_s,
            "origin": self.machine,
        }
        await self.redis.xadd(
            home_stream(home),
            {"call": json.dumps(call)},
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
        popped = await self.redis.blpop(
            [_reply_key(request_id)], timeout=max(1, round(self.timeout_s))
        )
        if popped is None:
            logger.warning(
                "home_call_unanswered", home=home, tool=name, run_id=str(principal.run_id)
            )
            raise HomeUnreachable(
                f"the machine running this run ({home}) did not answer within "
                f"{round(self.timeout_s)}s"
            )
        reply = json.loads(popped[1])
        if reply.get("ok"):
            return str(reply["text"])
        if reply.get("denied"):
            raise ToolScopeDenied(reply["error"])
        raise ToolError(reply["error"])


def build_home_routing(settings: Settings, redis: Any) -> HomeRouting | None:
    """Routing for this process, or ``None`` on a single box (no machine id, or no Redis)."""
    if not settings.machine_id or redis is None:
        return None
    return HomeRouting(
        redis=redis,
        machine=settings.machine_id,
        timeout_s=float(settings.shell_exec_timeout_max_s) + _FORWARD_SLACK_S,
    )


async def _answer(redis: Any, execute: LocalExecute, call: dict[str, Any]) -> None:
    name = call.get("name", "?")
    try:
        text = await execute(
            _principal_from_wire(call["principal"]), name, call.get("arguments") or {}
        )
        reply: dict[str, Any] = {"ok": True, "text": text}
    except ToolScopeDenied as exc:
        reply = {"ok": False, "denied": True, "error": str(exc)}
    except ToolError as exc:
        reply = {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — boundary translation, like ToolRegistry.call_tool
        logger.exception("home_call_failed", tool=name, error_type=type(exc).__name__)
        reply = {"ok": False, "error": f"tool {name!r} failed: {type(exc).__name__}"}
    key = _reply_key(call["request_id"])
    await redis.lpush(key, json.dumps(reply))
    await redis.expire(key, _REPLY_TTL_S)


async def serve_home_calls(
    redis: Any,
    *,
    machine: str,
    execute: LocalExecute,
    stop: asyncio.Event,
    block_ms: int = 1000,
) -> None:
    """Answer the calls forwarded to ``machine`` until ``stop`` is set."""
    stream = home_stream(machine)
    consumer = f"{machine}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        # From the start of the stream, not "$": a call sent before this machine's first
        # serve would otherwise be skipped. Anything stale is dropped by its deadline.
        await redis.xgroup_create(stream, _GROUP, id="0", mkstream=True)
    except Exception as exc:  # noqa: BLE001 — BUSYGROUP: another process created it first
        if "BUSYGROUP" not in str(exc):
            raise
    in_flight: set[asyncio.Task[None]] = set()
    logger.info("home_calls_serving", machine=machine)
    while not stop.is_set():
        try:
            batches = await redis.xreadgroup(
                _GROUP, consumer, {stream: ">"}, count=16, block=block_ms
            )
        except Exception:  # noqa: BLE001 — a Redis blip must not end the service
            logger.warning("home_calls_read_failed", machine=machine, exc_info=True)
            await asyncio.sleep(1)
            continue
        if not batches:
            # BLOCK is the wait; this only keeps a read that returns early from spinning.
            await asyncio.sleep(0.05)
            continue
        for _stream, entries in batches:
            for entry_id, fields in entries:
                # Acknowledged as read: a tool call is never run twice.
                await redis.xack(stream, _GROUP, entry_id)
                try:
                    call = json.loads(fields["call"])
                except (KeyError, ValueError):
                    logger.warning("home_call_malformed", entry_id=entry_id)
                    continue
                if float(call.get("deadline", 0)) < time.time():
                    logger.info("home_call_expired", tool=call.get("name"))
                    continue
                task = asyncio.create_task(_answer(redis, execute, call))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
    for task in list(in_flight):
        task.cancel()


__all__ = [
    "HOME_STREAM_PREFIX",
    "HomeRouting",
    "HomeUnreachable",
    "LocalExecute",
    "build_home_routing",
    "home_stream",
    "serve_home_calls",
]
