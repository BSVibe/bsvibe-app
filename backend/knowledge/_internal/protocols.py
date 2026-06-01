# bsvibe:stable-internal — modifications require a design doc update.
# Owners: knowledge/_internal
"""Cross-module Protocols used by knowledge sub-packages.

``RunnerLike`` and ``ContextBuilderLike`` are forward-declared with ``Any`` for
the context type — the concrete ``SkillContext`` lives in ``backend.extensions.skill`` and
is wired at runtime via dependency injection (Bundle S).
"""

from __future__ import annotations

from typing import Any, Protocol


class RunnerLike(Protocol):
    """Protocol for objects that can execute plugins or skills."""

    async def run(self, meta: Any, context: Any) -> dict[str, Any]: ...


class NotifyRunnerLike(Protocol):
    """Protocol for runners that support notification entrypoints."""

    async def run_notify(self, meta: Any, context: Any) -> dict[str, Any]: ...


class ContextBuilderLike(Protocol):
    """Protocol for callables that create a skill/plugin execution context."""

    def __call__(self, *, input_data: dict[str, Any] | None = None) -> Any: ...
