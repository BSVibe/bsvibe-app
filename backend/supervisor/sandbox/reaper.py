"""Background sandbox idle reaper. Lifted from BSNexus.

``DockerSandboxManager.reap_idle()`` tears down idle containers but
needs to be driven on a poll. ``sandbox_reaper_loop`` is started from
the FastAPI lifespan when a real manager exists.
"""

from __future__ import annotations

import asyncio

import structlog

from backend.supervisor.sandbox.protocol import SandboxManager

logger = structlog.get_logger(__name__)

REAP_INTERVAL_S: float = 300.0


async def sandbox_reaper_loop(
    manager: SandboxManager, *, interval_s: float = REAP_INTERVAL_S
) -> None:
    while True:
        try:
            await manager.reap_idle()
        except Exception:  # noqa: BLE001 — a reap failure must not stop the loop
            logger.exception("sandbox_reaper_failed")
        await asyncio.sleep(interval_s)
