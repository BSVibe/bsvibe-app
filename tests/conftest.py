"""Top-level pytest fixtures shared across the suite.

Goal: keep cross-test state from poisoning later tests. Specifically the
B16 / C2 :class:`backend.api.v1.live_events.LiveEventBus` is a process-wide
singleton; the SSE-redis-bus lift (C2) binds an :class:`asyncio.Redis`
client into it at app startup. In tests, ``create_app`` / ``run_workers``
get exercised within a per-test event loop — when that loop closes, the
singleton still holds a redis client tied to the now-dead loop, and the
next test's audit emit triggers a chain of "Task got Future attached to a
different loop" + "Event loop is closed" errors that escape via callbacks
into running tasks (mapped a Decision path to system_error in the executor
tests). The autouse fixture here resets the singleton state before AND
after each test so every test starts from a clean bus.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _reset_live_event_bus_singleton() -> Iterator[None]:
    """Clear the process-wide ``LiveEventBus`` state between tests.

    The bus is an in-process fan-out keyed by ``workspace_id`` plus an
    optional Redis pub/sub leg. Stale state — leftover subscriber queues
    bound to closed event loops, stale relay tasks, an old redis client
    tied to a dead loop — must not leak across tests.
    """
    # Lazy import: not every test needs the bus module loaded.
    from backend.api.v1 import live_events as _le

    def _reset() -> None:
        bus = _le._BUS
        if bus is not None:
            bus._subscribers.clear()
            # Cancel any leftover relay tasks; ignore close errors since the
            # owning event loop may already be torn down.
            for task in list(bus._relay_tasks.values()):
                try:
                    task.cancel()
                except Exception:  # noqa: BLE001 — best-effort teardown
                    pass
            bus._relay_tasks.clear()
            bus._redis = None
        _le._BUS = None

    _reset()
    yield
    _reset()
