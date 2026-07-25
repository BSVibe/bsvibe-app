"""Test-support: drain an executor chunk stream into a terminal result.

Production drains a streaming executor with ``_stream_and_collect`` /
``_StreamOutcome`` in :mod:`backend.executors.worker.main` (which also
publishes to Redis and captures the workspace). Executor unit tests only
need the aggregate ``success`` / ``stdout`` / ``error_message`` from the
chunk stream, so this tiny helper drains it directly — closing the async
generator in a ``finally`` so subprocess / tempfile cleanup runs before
the assertions, exactly like the production path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from backend.executors.worker.executors import ExecutionChunk


@dataclass
class DrainedResult:
    """Aggregate of a drained chunk stream."""

    success: bool
    stdout: str = ""
    error_message: str | None = None


async def drain(stream: AsyncIterator[ExecutionChunk]) -> DrainedResult:
    """Drain ``stream`` into a :class:`DrainedResult`, always closing it."""
    parts: list[str] = []
    error: str | None = None
    success = True
    try:
        async for chunk in stream:
            if chunk.delta:
                parts.append(chunk.delta)
            if chunk.error:
                error = chunk.error
                success = False
            if chunk.done:
                break
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001, S110 — cleanup best-effort
                pass
    return DrainedResult(success=success, stdout="".join(parts), error_message=error)
