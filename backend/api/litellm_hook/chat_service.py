"""ChatService — OpenAI-shape chat completions dispatcher.

Phase 1 skeleton — concrete lift of BSGateway ``bsgateway/chat/service.py``
follows Bundle G integration (needs the workers stream + RoutingLogsRepository
session context).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class ChatCompletionContext:
    workspace_id: uuid.UUID
    account_id: uuid.UUID | None
    trace_id: str
    stream: bool


class ChatService:
    """Owns the per-request chat-completions dispatch flow.

    For Phase 1 this surface returns 501-style ``NotImplementedError``;
    the agent loop / orchestrator code (Bundle X, Bundle G) wires through
    the same surface so swapping in the full lift is mechanical.
    """

    def __init__(self) -> None:
        pass

    async def complete(
        self,
        *,
        context: ChatCompletionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Non-streaming dispatch — returns full completion dict."""
        # TODO(bundle-api-integration): lift bsgateway/chat/service.py.
        logger.debug(
            "chat_service_complete_stub",
            workspace_id=str(context.workspace_id),
            trace_id=context.trace_id,
        )
        raise NotImplementedError("ChatService.complete is a Bundle API skeleton")

    async def stream(
        self,
        *,
        context: ChatCompletionContext,
        payload: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming dispatch — yields SSE chunks."""
        # TODO(bundle-api-integration): lift bsgateway/chat/service.py streaming path.
        logger.debug(
            "chat_service_stream_stub",
            workspace_id=str(context.workspace_id),
            trace_id=context.trace_id,
        )
        if False:  # pragma: no cover — placeholder to make this an async generator
            yield {}
        raise NotImplementedError("ChatService.stream is a Bundle API skeleton")
