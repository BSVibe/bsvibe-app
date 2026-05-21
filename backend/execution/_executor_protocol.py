"""Minimal ``ExecutorClient`` Protocol — typing-only stub for lifted modules.

The full BSNexus ``backend.src.core.executor_config.protocol.ExecutorClient``
hasn't been ported yet (it carries a dispatcher registry + retry/backoff
metadata that's out of scope for Bundle 1). The decomposer + verifier
judge only touch the ``.execute(...)`` method, so we type against this
narrow Protocol until the full lift lands.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExecutorClient(Protocol):
    """Narrow surface used by decomposer / judge LLM calls."""

    async def execute(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Dispatch a prompt to the configured executor; return a free-shape result."""
