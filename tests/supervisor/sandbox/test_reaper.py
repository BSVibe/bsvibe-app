"""Tests for the background sandbox reaper loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from backend.workflow.infrastructure.sandbox import sandbox_reaper_loop


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
        task = asyncio.create_task(sandbox_reaper_loop(mgr, interval_s=0.005))
        # Wait until the loop has cleared the first (failing) attempt + a
        # subsequent (no-op) attempt. A fixed 50ms sleep was passing on the
        # Linux CI runner but consistently failing on macOS local: structlog's
        # exception formatting under coverage left the loop with only 1 await
        # done before the test's cancel fired. Poll for the asserted state
        # instead of trusting wall-clock scheduling.
        for _ in range(200):  # ≤ ~1s cap
            if mgr.reap_idle.await_count >= 2:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # The loop must have made multiple attempts despite the first failure.
        assert mgr.reap_idle.await_count >= 2
