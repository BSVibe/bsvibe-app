"""OpenAI-compatible chat completions endpoint.

Wires :class:`backend.api.litellm_hook.chat_service.ChatService` against the
existing :class:`backend.router.dispatch.GatewayDispatcher`. The dispatcher
itself is constructed per-request from the request-scoped AsyncSession +
the workspace's ModelAccountService + Classifier + BudgetPolicyService +
LlmClient.

The endpoint surface:

    POST /api/v1/chat/completions
    Body: OpenAI-shape + optional ``metadata.bsvibe_account_id`` +
          ``metadata.bsvibe_model_account_id``
    Returns: OpenAI-shape completion + ``bsvibe`` metadata

Auth dependency (workspace_id) intentionally fails fast with 501 until
Bundle G wires backend.shared.authz; that's how the route signals "real
backend wired, but auth path not yet productionized."
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id, require_account_id
from backend.api.litellm_hook.audit_events import (
    GatewayCompletionDispatched,
    GatewayCompletionFailed,
)
from backend.api.litellm_hook.chat_service import ChatCompletionContext, ChatService
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.router.accounts.service import ModelAccountService
from backend.router.budget.errors import BudgetExceeded
from backend.router.budget.policy import BudgetPolicyService
from backend.router.budget.repository import BudgetPolicyRepository
from backend.router.budget.tracker import BudgetTracker, InMemoryBudgetStore
from backend.router.classifier.local_vs_cloud import LocalVsCloudClassifier
from backend.router.classifier.static import StaticClassifier
from backend.router.dispatch import (
    DispatchError,
    GatewayDispatcher,
    ModelAccountNotFound,
)
from backend.router.llm_client import LlmClient
from backend.supervisor.audit.events import AuditActor, AuditResource
from backend.supervisor.audit.service import safe_emit

router = APIRouter()


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str = Field(..., description="system | user | assistant | tool")
    content: Any


class ChatCompletionMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")
    bsvibe_account_id: uuid.UUID | None = None
    bsvibe_model_account_id: uuid.UUID | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    metadata: ChatCompletionMetadata | None = None


def _build_dispatcher(session: AsyncSession) -> GatewayDispatcher:
    cipher = CredentialCipher(_key_from_settings())
    accounts = ModelAccountService(session, cipher=cipher)
    budget_repo = BudgetPolicyRepository(session)
    tracker = BudgetTracker(InMemoryBudgetStore())
    budget = BudgetPolicyService(repository=budget_repo, tracker=tracker)
    from backend.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    classifier = LocalVsCloudClassifier(
        local_score_max=settings.gateway_local_score_max,
        cloud_score_min=settings.gateway_cloud_score_min,
        static=StaticClassifier(
            local_score_max=settings.gateway_local_score_max,
            cloud_score_min=settings.gateway_cloud_score_min,
        ),
    )
    llm = LlmClient()
    return GatewayDispatcher(accounts=accounts, classifier=classifier, budget=budget, llm=llm)


@router.post("/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    """OpenAI-shape chat completions — dispatches via backend.router."""
    md = payload.metadata or ChatCompletionMetadata()
    model_account_id = md.bsvibe_model_account_id
    if model_account_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="metadata.bsvibe_model_account_id required (no provider auto-select in Phase 1)",
        )

    service = ChatService(dispatcher=_build_dispatcher(session))
    ctx = ChatCompletionContext(
        workspace_id=workspace_id,
        account_id=account_id,
        trace_id=str(uuid.uuid4()),
        stream=payload.stream,
        model_account_id=model_account_id,
        estimated_cost_cents=0,
    )
    body = payload.model_dump()
    actor = AuditActor(type="user", id=str(account_id))
    resource = AuditResource(type="model_account", id=str(model_account_id))
    try:
        completion = await service.complete(context=ctx, payload=body)
    except ModelAccountNotFound as exc:
        await safe_emit(
            GatewayCompletionFailed(
                actor=actor,
                workspace_id=str(workspace_id),
                trace_id=ctx.trace_id,
                resource=resource,
                data={"error": "model_account_not_found", "detail": str(exc)},
            ),
            session=session,
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BudgetExceeded as exc:
        await safe_emit(
            GatewayCompletionFailed(
                actor=actor,
                workspace_id=str(workspace_id),
                trace_id=ctx.trace_id,
                resource=resource,
                data={"error": "budget_exceeded", "detail": str(exc)},
            ),
            session=session,
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    except DispatchError as exc:
        await safe_emit(
            GatewayCompletionFailed(
                actor=actor,
                workspace_id=str(workspace_id),
                trace_id=ctx.trace_id,
                resource=resource,
                data={"error": "dispatch_error", "detail": str(exc)},
            ),
            session=session,
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    await safe_emit(
        GatewayCompletionDispatched(
            actor=actor,
            workspace_id=str(workspace_id),
            trace_id=ctx.trace_id,
            resource=resource,
            data={
                "model": completion.get("model"),
                "actual_cost_cents": completion.get("bsvibe", {}).get("actual_cost_cents"),
                "classification_tier": completion.get("bsvibe", {}).get("classification_tier"),
            },
        ),
        session=session,
    )
    await session.commit()
    return completion
