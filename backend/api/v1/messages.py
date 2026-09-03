"""/api/v1/messages — founder-direct submission into the workflow.

The Direct path entrypoint (Workflow §11.1). A founder types a request and
we land it on the workflow exactly as an inbound webhook would, via
:class:`backend.workflow.application.intake.direct.DirectTrigger` (``source="direct"``). The
agent workers (intake → agent → delivery) drive it the rest of the way to
a delivered artifact — this surface only accepts the trigger.

Write-only POST: the run is *created* by the worker pipeline, never by the
HTTP request, so this returns an acceptance receipt, not a run.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user_row, get_db_session, get_workspace_id
from backend.config import get_settings
from backend.identity.db import UserRow
from backend.identity.product_resolution import resolve_product_for_workspace
from backend.workers.emit import (
    STREAM_INTAKE,
    emit_stream_notification,
    get_dispatch_redis_client,
    get_emit_redis_client,
)
from backend.workflow.application.direct_answer import DirectAnswerService
from backend.workflow.application.intake.direct import DirectTrigger
from backend.workflow.application.run_caps import RunCapReached, enforce_run_cap

router = APIRouter()


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=20000)
    product_id: uuid.UUID | None = None
    trace_id: str | None = Field(default=None, max_length=64)


class MessageAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    duplicate: bool
    workspace_id: uuid.UUID


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=20000)
    #: L10 grounding — the product the founder is asking ABOUT. Optional: a
    #: general question needs no product, but "how's the project?" must be
    #: grounded in this product's deliverables + knowledge, not answered as if
    #: the workspace were empty. Validated against the workspace in the service
    #: (a foreign / unknown id degrades to an ungrounded answer, never a 400 —
    #: the inline path must not fail a question).
    product_id: uuid.UUID | None = None


class AskResponse(BaseModel):
    """L10 — the inline Direct-question answer. ``answered=False`` means the text
    is NOT a question (or no chat model resolved) → the caller dispatches it as
    work via ``POST /api/v1/messages`` instead."""

    model_config = ConfigDict(extra="forbid")

    answered: bool
    answer: str | None = None


async def _resolve_product_id(
    *,
    workspace_id: uuid.UUID,
    requested: uuid.UUID | None,
    session: AsyncSession,
) -> uuid.UUID:
    """L-P1 규칙은 :func:`~backend.identity.product_resolution.
    resolve_product_for_workspace` 가 소유한다 — 여기서는 REST 의 오류 표면만 얹는다.

    (규칙: 명시 제품 → 가장 먼저 만들어진 제품 → 없음. 다른 워크스페이스의 id 는
    조용히 기본값으로 떨어진다.)
    """
    product_id = await resolve_product_for_workspace(
        session,
        workspace_id=workspace_id,
        slug_or_id=str(requested) if requested is not None else None,
    )
    if product_id is None:
        raise HTTPException(
            status_code=400,
            detail="workspace has no products — create one before submitting a message",
        )
    return product_id


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def submit_message(
    body: MessageCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    user_row: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> MessageAccepted:
    """Accept a founder-direct message → TriggerEvent (``source="direct"``).

    Idempotent on ``(founder_id, text)`` (see :class:`DirectTrigger`): a
    double-submit of the same text by the same founder collapses and is
    reported via ``duplicate=True`` rather than landing a second trigger.

    L-P1: every direct message is bound to a product. When ``product_id``
    is omitted, the workspace's earliest-created product is the default;
    a workspace with zero products receives a 400 so the founder creates
    one before submitting (no more NULL runs that vanish from project
    detail pages).
    """
    product_id = await _resolve_product_id(
        workspace_id=workspace_id, requested=body.product_id, session=session
    )

    # The free plan's concurrent-run cap. 429 (not 400) because the PWA reads
    # a 400 here as "this workspace has no products" — the one thing this
    # endpoint refused before — and would have shown a founder who has plenty
    # of products a hint telling them to make one.
    try:
        await enforce_run_cap(session, workspace_id=workspace_id)
    except RunCapReached as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            # A structured detail, not a sentence: the PWA writes its own
            # localized copy (an English string would land verbatim on a KO
            # surface) and needs the real limit, which differs per workspace.
            detail={"code": "run_cap_reached", "limit": exc.limit, "held": exc.held},
        ) from exc

    trigger = DirectTrigger(session)
    outcome = await trigger.submit(
        workspace_id=workspace_id,
        founder_id=user_row.id,
        text=body.text,
        product_id=product_id,
        trace_id=body.trace_id,
    )
    await session.commit()

    # AFTER the TriggerEvent is durable, wake the IntakeWorker consumer on the
    # ``intake`` stream. Gated (no-op + no Redis client built in db_polling) and
    # soft-fail (a Redis hiccup never breaks the accepted POST — the committed
    # TriggerEvent is the source of truth, picked up by DB-polling regardless).
    # A duplicate (collapsed) submit landed no new row, so it emits nothing.
    if not outcome.duplicate:
        settings = get_settings()
        await emit_stream_notification(
            get_emit_redis_client(settings),
            settings=settings,
            stream=STREAM_INTAKE,
            fields={"workspace_id": str(workspace_id)},
        )

    return MessageAccepted(
        accepted=True,
        duplicate=outcome.duplicate,
        workspace_id=workspace_id,
    )


@router.post("/ask")
async def ask_message(
    body: AskRequest,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AskResponse:
    """L10 (#4/#5) — answer a founder's Direct *question* INLINE, synchronously.

    A question is answered from workspace knowledge with a CHAT model and
    returned right here — no run, no executor. Whether the text IS a question is
    the model's call (the ASK-vs-PRODUCE rubric it shares with the frame stage),
    made inside the same completion that writes the answer — there is no keyword
    pre-gate: word lists read grammar, not intent, and sent "현 프로젝트 상황
    설명해줘" to a coding executor (prod run ff1615e8). ``answered=False`` when the
    model reads the text as work, no chat model is configured, or the inline
    attempt fails; the PWA then falls back to ``POST /api/v1/messages`` (the
    normal async dispatch), where the frame stage classifies it again.
    """
    settings = get_settings()
    # Thread the dispatch redis client so an executor-routed chat account can be
    # served inline too (functional parity with LiteLLM); None when no redis_url
    # is configured → the inline answer degrades to async dispatch.
    service = DirectAnswerService(
        session, settings=settings, redis=get_dispatch_redis_client(settings)
    )
    answer = await service.answer(
        workspace_id=workspace_id, product_id=body.product_id, text=body.text
    )
    if answer is None or not answer.strip():
        # No chat account resolved (or an empty answer) — let the caller dispatch
        # it as work rather than showing a blank inline reply.
        return AskResponse(answered=False)
    return AskResponse(answered=True, answer=answer.strip())


__all__ = ["AskRequest", "AskResponse", "MessageAccepted", "MessageCreate", "router"]
