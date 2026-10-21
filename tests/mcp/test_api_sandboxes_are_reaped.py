"""#929 — a sandbox the API process made for a run's tool calls does not outlive its use.

The MCP work tools create (or adopt) the run's sandbox in the API process. ``reap_idle``
releases a box that sat unused for ``sandbox_idle_reap_seconds`` — but nothing ever started
its loop (``reaper.py`` said "started from the FastAPI lifespan"; it was not). On one box that
was masked: the worker adopted and released the box when the run resumed there. Once a run can
move to another machine, a box made here for it is never touched again — it keeps running and
keeps one of this process's ``sandbox_max_concurrent`` slots, and after a couple of those every
new box waits forever.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.mcp import lifespan

from .._support import db_engine


@pytest.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.parametrize("enabled", [True, False], ids=["sandbox", "no-sandbox"])
async def test_the_api_runs_the_sandbox_reaper_for_its_manager(sf, monkeypatch, enabled) -> None:
    manager = object() if enabled else None
    reaped: list[Any] = []
    stopped = asyncio.Event()

    async def _loop(m: Any, **_: Any) -> None:
        reaped.append(m)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(lifespan, "get_sandbox_manager", lambda: manager)
    monkeypatch.setattr(lifespan, "sandbox_reaper_loop", _loop)

    async with lifespan.mcp_lifespan(FastAPI(), session_factory=sf):
        await asyncio.sleep(0)

    if not enabled:
        assert reaped == []
        return
    assert reaped == [manager]
    assert stopped.is_set(), "stopped with the app"
