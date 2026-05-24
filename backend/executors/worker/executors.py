"""Executor protocol, chunk/result dataclasses, capability detection + factory.

Kept self-contained so the worker package never depends on the backend's
server-side modules (no SQLAlchemy, no FastAPI). An executor is a one-shot
subprocess streamer — ``execute()`` returns an ``AsyncIterator[ExecutionChunk]``
reading a CLI's native JSON stream; the worker main loop forwards each chunk to
the backend (optionally via Redis pub/sub) and aggregates the final output.

This lift ships only the ``claude_code`` executor. ``codex`` / ``opencode`` are
*detectable* (so the worker registers the right capabilities) but their
executors are a follow-up lift — :func:`select_executor` raises for them.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from backend.executors.worker.claude_code import ClaudeCodeExecutor


@dataclass
class ExecutionChunk:
    """One incremental message from a streaming executor.

    ``delta`` carries new text to append to the running output; ``done`` marks
    terminal end-of-stream (with optional ``error``). ``raw`` keeps the parsed
    source event for debugging / future structured forwarding.
    """

    delta: str = ""
    done: bool = False
    error: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class ExecutionResult:
    """Aggregated terminal result, built by :func:`collect` from chunks."""

    success: bool
    stdout: str = ""
    error_message: str | None = None
    error_category: Literal["environment", "tool", ""] = ""
    chunks: list[ExecutionChunk] = field(default_factory=list)


@runtime_checkable
class ExecutorProtocol(Protocol):
    """A streaming CLI executor."""

    def execute(self, prompt: str, context: dict[str, Any]) -> AsyncIterator[ExecutionChunk]: ...

    def supported_task_types(self) -> list[str]: ...


async def collect(stream: AsyncIterator[ExecutionChunk]) -> ExecutionResult:
    """Drain a chunk stream into an :class:`ExecutionResult`.

    Always closes the underlying async generator in a ``finally`` so subprocess
    cleanup / tempfile unlink runs synchronously before returning.
    """
    parts: list[str] = []
    chunks: list[ExecutionChunk] = []
    error: str | None = None
    success = True
    try:
        async for chunk in stream:
            chunks.append(chunk)
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
    return ExecutionResult(
        success=success,
        stdout="".join(parts),
        error_message=error,
        error_category="" if success else "tool",
        chunks=chunks,
    )


# ── Capability detection ──────────────────────────────────────────────────────

# Probe order matters: claude_code is the only capability with a real executor
# in this lift, so it leads. codex / opencode are detectable for forward
# compatibility (a follow-up lift adds their executors).
_CLI_CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("claude", "claude_code"),
    ("codex", "codex"),
    ("opencode", "opencode"),
)


def detect_capabilities() -> list[str]:
    """Return the executor capabilities available on this machine (PATH probe)."""
    caps: list[str] = []
    for cmd, capability in _CLI_CAPABILITIES:
        if shutil.which(cmd):
            caps.append(capability)
    return caps


# ── Factory ───────────────────────────────────────────────────────────────────


def select_executor(executor_type: str) -> ExecutorProtocol:
    """Create an :class:`ExecutorProtocol` for ``executor_type``.

    Only ``claude_code`` has a real executor in this lift; ``codex`` /
    ``opencode`` (and anything unknown) raise :class:`ValueError`.
    """
    if executor_type == "claude_code":
        # Imported lazily to avoid a circular import (claude_code imports the
        # chunk/result types from this module).
        from backend.executors.worker.claude_code import (  # noqa: PLC0415 — breaks an import cycle
            ClaudeCodeExecutor,
        )

        executor: ClaudeCodeExecutor = ClaudeCodeExecutor()
        return executor
    raise ValueError(f"Unsupported executor type: {executor_type!r} (no executor in this lift)")


__all__ = [
    "ExecutionChunk",
    "ExecutionResult",
    "ExecutorProtocol",
    "collect",
    "detect_capabilities",
    "select_executor",
]
