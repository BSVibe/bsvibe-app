"""OpenAI-compatible chat completions endpoint.

Wires :mod:`backend.gateway.dispatch` into HTTP. The LiteLLM
``async_pre_call_hook`` (Bundle 1.5c) fires before the upstream call to
evaluate routing rules, budget caps, and account resolution.

POST /api/v1/chat/completions — OpenAI shape (+ optional ``metadata``
with ``bsvibe_account_id`` / ``bsvibe_project_id``). Streaming SSE.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str = Field(..., description="system | user | assistant | tool")
    content: str | list[dict[str, Any]]


class ChatCompletionMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")
    bsvibe_account_id: str | None = None
    bsvibe_project_id: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    metadata: ChatCompletionMetadata | None = None


@router.post("/completions")
async def chat_completions(payload: ChatCompletionRequest) -> dict[str, Any]:
    """OpenAI-shape chat completions — dispatches via backend.gateway."""
    # TODO(bundle-api-integration): wire to backend.api.litellm_hook.chat_service.
    # Flow (per plan §4):
    # 1. Extract (workspace_id, account_id) from JWT + metadata
    # 2. async_pre_call_hook → RuleEngine.evaluate → budget check → account resolve
    # 3. classifier.classify (informational tier hint)
    # 4. backend.gateway.dispatch.GatewayDispatcher.dispatch
    # 5. Stream via SSE
    # 6. RoutingLogsRepository.insert_routing_log
    # 7. supervisor.audit.safe_emit
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="chat completions dispatch not yet wired (Bundle API skeleton)",
    )
