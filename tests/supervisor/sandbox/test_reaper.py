"""Tests for the background sandbox reaper loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from backend.supervisor.sandbox import sandbox_reaper_loop


class TestReaperLoop:
    async def test_calls_reap_idle_repeatedly(self):
        mgr = AsyncMock()
        mgr.reap_idle = AsyncMock()
        task = asyncio.create_task(sandbox_reaper_loop(mgr, interval_s=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert mgr.reap_idle.await_count >= 1

    async def test_reap_failure_does_not_stop_loop(self):
        mgr = AsyncMock()
        mgr.reap_idle = AsyncMock(side_effect=[RuntimeError("dind hiccup"), None, None, None])
        task = asyncio.create_task(sandbox_reaper_loop(mgr, interval_s=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # The loop must have made multiple attempts despite the first failure.
        assert mgr.reap_idle.await_count >= 2
