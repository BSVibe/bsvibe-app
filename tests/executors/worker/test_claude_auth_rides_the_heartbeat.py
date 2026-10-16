"""The worker's Claude sign-in verdict rides its heartbeat (outage 2026-10-15).

The keepalive already classifies a revoked grant (``definitive_failure``) and has always logged
it — hourly, unread, for ~17 hours. Now it records the verdict in a :class:`ClaudeAuthState`
that ``run_once`` reads on every heartbeat and sends as ``X-BSVibe-Worker-Claude-Auth``. The
backend turns the transition into a founder alert (``tests/api/test_worker_auth_alert.py``).

A header, not a body field: ``HeartbeatBody`` is ``extra="forbid"`` and an older backend would
422 every heartbeat — the same reason ``X-BSVibe-Worker-Protocol`` is a header.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from backend.executors.worker import main as worker_main
from backend.executors.worker.claude_auth import ClaudeBearerResult
from backend.executors.worker.config import WorkerSettings

_HEADER = "x-bsvibe-worker-claude-auth"


def _settings(**overrides: Any) -> WorkerSettings:
    base: dict[str, Any] = {
        "server_url": "http://test",
        "token": "WORKER-TOKEN",
        "access_token": "",
        "name": "test-worker",
        "redis_url": "",
        "poll_interval_seconds": 0,
        "max_parallel_tasks": 3,
        "poll_batch_max": 5,
        "claude_auth_refresh_interval_s": 0,
    }
    base.update(overrides)
    return WorkerSettings(**base)


def _client(beats: list[str | None]) -> httpx.AsyncClient:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/workers/heartbeat":
            beats.append(request.headers.get(_HEADER))
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/workers/poll":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://test")


_DEAD = ClaudeBearerResult("BORROWED-CLI-TOKEN", definitive_failure=True)
_OK = ClaudeBearerResult("FRESH-ACCESS-TOKEN")


async def _keepalive(results: list[ClaudeBearerResult], state: Any) -> None:
    stop = asyncio.Event()
    queue = list(results)

    def _resolve() -> ClaudeBearerResult:
        result = queue.pop(0)
        if not queue:
            stop.set()
        return result

    await asyncio.wait_for(
        worker_main._claude_auth_keepalive_loop(
            settings=_settings(), stop=stop, resolve=_resolve, auth_state=state
        ),
        timeout=5,
    )


async def test_the_keepalive_records_a_revoked_grant_as_failed() -> None:
    state = worker_main.ClaudeAuthState()

    await _keepalive([_DEAD], state)

    assert state.verdict == "failed"


async def test_a_fresh_sign_in_turns_it_back_to_ok() -> None:
    """``bsvibe-worker claude-login`` writes a new credential; the next keepalive must say so,
    or the founder's all-clear never comes."""
    state = worker_main.ClaudeAuthState()

    await _keepalive([_DEAD, _OK], state)

    assert state.verdict == "ok"


async def test_a_token_borrowed_after_a_revoked_grant_is_not_health() -> None:
    """#970: the CLI fallback hands back a working token while the worker's own credential is
    dead — the verdict follows ``definitive_failure``, not "got a token"."""
    state = worker_main.ClaudeAuthState()

    await _keepalive([_OK, _DEAD], state)

    assert state.verdict == "failed"


async def test_the_heartbeat_carries_the_verdict() -> None:
    state = worker_main.ClaudeAuthState()
    state.verdict = "failed"
    beats: list[str | None] = []

    async with _client(beats) as client:
        await worker_main.run_once(
            client=client,
            settings=_settings(),
            executors={},
            headers={"X-Worker-Token": "T"},
            redis=None,
            in_flight=set(),
            claude_auth=state,
        )

    assert beats == ["failed"]


async def test_no_verdict_means_no_header() -> None:
    """A worker that has not checked yet — or runs no Claude at all — claims nothing."""
    beats: list[str | None] = []

    async with _client(beats) as client:
        await worker_main.run_once(
            client=client,
            settings=_settings(),
            executors={},
            headers={"X-Worker-Token": "T"},
            redis=None,
            in_flight=set(),
            claude_auth=worker_main.ClaudeAuthState(),
        )

    assert beats == [None]


async def test_the_real_keepalive_and_heartbeat_share_one_verdict(monkeypatch: Any) -> None:
    """Wire: ``poll_and_execute`` must hand the SAME state to the keepalive it starts and to
    ``run_once`` — two separate objects would leave every heartbeat silent."""
    monkeypatch.setattr(worker_main, "detect_capabilities", lambda: ["claude_code"])
    monkeypatch.setattr(worker_main, "resolve_claude_bearer", lambda: _DEAD)
    stop = asyncio.Event()
    beats: list[str | None] = []
    original = worker_main.run_once

    async def _counted(**kwargs: Any) -> Any:
        result = await original(**kwargs)
        if len(beats) >= 4:
            stop.set()
        await asyncio.sleep(0.01)  # let the keepalive task take its turn
        return result

    monkeypatch.setattr(worker_main, "run_once", _counted)
    async with _client(beats) as client:
        await asyncio.wait_for(
            worker_main.poll_and_execute(
                settings=_settings(), client=client, redis=None, stop=stop
            ),
            timeout=10,
        )

    assert "failed" in beats, f"no heartbeat carried the verdict: {json.dumps(beats)}"
