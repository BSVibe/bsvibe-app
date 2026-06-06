"""Bridge from FrameLlm / other workflow seams to backend.dispatch (Lift E1).

The new :class:`backend.dispatch.resolver.ModelAccountResolver` returns
adapters whose only verb is :meth:`chat`. Existing workflow seams
(``FrameLlm.complete_text`` today, more verbs to come) speak their own
shape; this module is the thin one-method bridge.

Lives in :mod:`backend.workflow.application.runtime` because it is a
workflow-runtime concern (the call site is in the agent runtime
factory). Lift E2 collapses the bridge entirely when ``complete_text``
is replaced by direct ``chat`` calls at every call site.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.workflow.application.stages.frame import FrameLlm

logger = structlog.get_logger(__name__)


__all__ = [
    "_ResolverFrameLlm",
    "_resolve_frame_via_new_path",
]


async def _resolve_frame_via_new_path(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    settings: Settings,
) -> FrameLlm | None:
    """E1 new-path resolution for the frame stage.

    Calls :class:`backend.dispatch.resolver.ModelAccountResolver` with
    caller_id ``workflow.frame``. Returns a :class:`FrameLlm` adapter on
    a hit; ``None`` on
    :class:`~backend.dispatch.resolver.NoMatchingRouteError` so the
    legacy single-active-native fallback in the agent_runtime factory
    still runs. The classifier features for the frame stage are kept
    here for the legacy gateway path; E2 deletes them.
    """
    from backend.dispatch.resolver import (  # noqa: PLC0415
        ModelAccountResolver,
        NoMatchingRouteError,
    )
    from backend.router.classifier.base import ClassificationFeatures  # noqa: PLC0415

    resolver = ModelAccountResolver(session, settings=settings)
    try:
        resolved = await resolver.resolve_for(
            caller_id="workflow.frame",
            workspace_id=workspace_id,
            legacy_features=ClassificationFeatures(
                token_count=512,
                system_prompt_chars=1024,
                conversation_turns=1,
                code_block_count=0,
                tool_count=0,
            ),
            legacy_projected_cost_cents=1,
        )
    except NoMatchingRouteError:
        return None
    except KeyError:
        return None
    return _ResolverFrameLlm(adapter=resolved.adapter)


class _ResolverFrameLlm:
    """Bridge :class:`FrameLlm.complete_text` to the new adapter chain."""

    def __init__(self, *, adapter: Any) -> None:
        self._adapter = adapter

    async def complete_text(self, *, system: str, user: str) -> str:
        response = await self._adapter.chat(
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=None,
        )
        return str(response.content)
