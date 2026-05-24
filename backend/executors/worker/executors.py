"""Executor protocol, chunk/result dataclasses, capability detection + factory.

Kept self-contained so the worker package never depends on the backend's
server-side modules (no SQLAlchemy, no FastAPI). An executor is a one-shot
subprocess streamer — ``execute()`` returns an ``AsyncIterator[ExecutionChunk]``
reading a CLI's native JSON stream; the worker main loop forwards each chunk to
the backend (optionally via Redis pub/sub) and aggregates the final output.

The ``claude_code``, ``codex``, and ``opencode`` executors each wrap their
native CLI; :func:`detect_capabilities` PATH-probes all three and
:func:`select_executor` maps an executor_type string to the matching instance.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from backend.executors.worker.claude_code import ClaudeCodeExecutor
    from backend.executors.worker.codex import CodexExecutor
    from backend.executors.worker.opencode import OpenCodeExecutor


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


# ── Subprocess env sanitization ─────────────────────────────────────────────

# Parent Claude-Code *session/agent* markers. When the worker daemon is launched
# from inside a Claude Code session (or any env that exported these), a spawned
# ``claude`` CLI attaches to / is confused by that session and fails (observed:
# ``API Error: 400 role 'system' is not supported on this model``). Each CLI must
# run as a clean standalone process, so we strip these before spawning.
#
# Conservative policy: drop any key with the ``CLAUDE_CODE_`` prefix (the session
# markers — CLAUDE_CODE_SESSION_ID / CLAUDE_CODE_ENTRYPOINT / …) plus the named
# extras below. We do NOT touch PATH, HOME, ANTHROPIC_* auth, or other CLAUDE_*
# config (e.g. CLAUDE_CONFIG_DIR) — only the session/agent leakage.
_SESSION_ENV_PREFIX = "CLAUDE_CODE_"
_SESSION_ENV_KEYS: frozenset[str] = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_EFFORT",
        "AI_AGENT",
    }
)


def sanitized_subprocess_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of ``base`` (default ``os.environ``) with session leakage removed.

    Strips every key starting with ``CLAUDE_CODE_`` plus the named extras
    (``CLAUDECODE``, ``CLAUDE_AGENT_SDK_VERSION``, ``CLAUDE_EFFORT``, ``AI_AGENT``)
    so a CLI subprocess runs as a clean standalone process. All other env —
    PATH, HOME, ANTHROPIC_* auth, other CLAUDE_* config — is preserved.
    """
    source: Mapping[str, str] = base if base is not None else os.environ
    return {
        key: value
        for key, value in source.items()
        if not key.startswith(_SESSION_ENV_PREFIX) and key not in _SESSION_ENV_KEYS
    }


# ── Capability detection ──────────────────────────────────────────────────────

# Probe order matters: claude_code leads (the worker's primary executor),
# followed by codex / opencode. Each capability has a real executor wired into
# :func:`select_executor`.
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

    Wires ``claude_code`` / ``codex`` / ``opencode``; anything unknown raises
    :class:`ValueError`. Executors are imported lazily to avoid a circular
    import (each imports the chunk/result types from this module).
    """
    if executor_type == "claude_code":
        from backend.executors.worker.claude_code import (  # noqa: PLC0415 — breaks an import cycle
            ClaudeCodeExecutor,
        )

        claude: ClaudeCodeExecutor = ClaudeCodeExecutor()
        return claude
    if executor_type == "codex":
        from backend.executors.worker.codex import (  # noqa: PLC0415 — breaks an import cycle
            CodexExecutor,
        )

        codex: CodexExecutor = CodexExecutor()
        return codex
    if executor_type == "opencode":
        from backend.executors.worker.opencode import (  # noqa: PLC0415 — breaks an import cycle
            OpenCodeExecutor,
        )

        opencode: OpenCodeExecutor = OpenCodeExecutor()
        return opencode
    raise ValueError(f"Unsupported executor type: {executor_type!r}")


__all__ = [
    "ExecutionChunk",
    "ExecutionResult",
    "ExecutorProtocol",
    "collect",
    "detect_capabilities",
    "sanitized_subprocess_env",
    "select_executor",
]
