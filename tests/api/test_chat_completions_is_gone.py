"""``/api/v1/chat/completions`` is gone (#949 item 1 — 형님 decision 2026-10-16: delete).

An OpenAI-shaped proxy left over from the BSGateway era. Measured 2026-10-16: no caller in the
repo (PWA, MCP, SDK), zero requests in prod logs since 2026-10-08 (every archived backend log),
and on a workspace whose accounts are all executors — the founder's — it 500'd
(``reveal_api_key`` raises for ``provider="executor"``). Ahead of a public beta it was an
authenticated surface nobody used, so it was deleted rather than repaired.

What these pin is the BEHAVIOUR (no route answers; the caller cannot be targeted any more),
not the absence of spellings — and a control proves the router this test hits is the live one.
"""

from __future__ import annotations

import importlib

import httpx
import pytest

from backend.api.main import create_app
from backend.dispatch.caller_registry import KNOWN_CALLERS

pytestmark = pytest.mark.asyncio


async def _post(path: str) -> int:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(path, json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        return r.status_code


async def test_no_route_answers_chat_completions() -> None:
    assert await _post("/api/v1/chat/completions") == 404


async def test_control_the_v1_router_is_mounted() -> None:
    """Without this, a 404 above would also come from an app that mounted no v1 routes at all.
    An authenticated v1 route answers 401 to an anonymous caller — it exists."""
    assert await _post("/api/v1/messages") == 401


def test_the_chat_modules_are_gone() -> None:
    for name in ("backend.api.v1.chat", "backend.api.v1.chat_service"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)


def test_a_routing_rule_can_no_longer_target_the_caller() -> None:
    """Nothing calls ``chat.completions`` any more, so a rule aimed at it would never match —
    the write path refuses it instead of storing a dead rule."""
    from backend.api.v1.run_routing import _validate_caller_id

    assert "chat.completions" not in KNOWN_CALLERS
    with pytest.raises(ValueError, match="unknown caller_id"):
        _validate_caller_id("chat.completions")
    assert _validate_caller_id("workflow.frame") == "workflow.frame"  # control
