"""SkillContext helpers shared by the adapter + github delivery (Lift §17.7).

Connector outbound functions only read ``context.credentials`` /
``context.config`` (a delivery is a single REST call, not an agent loop), but
:class:`SkillContext` requires a non-None ``llm``. A no-op LLM that raises on
call keeps the contract honest (calling it from delivery code is a bug).
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.extensions.plugin.context import SkillContext

logger = structlog.get_logger(__name__)


class _NoLlm:
    """A no-op LLM for the outbound SkillContext.

    Connector outbound functions only read ``context.credentials`` /
    ``context.config`` (the delivery is a single REST call, not an agent loop),
    but :class:`SkillContext` requires a non-None ``llm``. Calling it is a bug,
    so it raises rather than silently no-opping.
    """

    async def chat(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("connector outbound delivery must not call the LLM")


def _build_context(*, credentials: dict[str, Any], config: dict[str, Any]) -> SkillContext:
    return SkillContext(
        llm=_NoLlm(),
        config=config,
        logger=logger,
        credentials=credentials,
    )


__all__ = ["_NoLlm", "_build_context"]
