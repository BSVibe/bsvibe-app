"""/api/v1/intents — author + list intent definitions (NL routing Lift N2).

Intent definitions are the SEMANTIC categories the N1
:class:`~backend.router.routing.run_routing.intent_classifier.IntentClassifier`
matches incoming work against. The founder names a category ("marketing",
"design", "complex-coding") and gives a few example phrases; the examples get
embedded so the classifier can route by the NATURE of the work, not just the
fixed execution-stage callers.

Intents are scoped to the workspace's ``(workspace_id, account_id)`` — the
personal account resolved by :func:`require_account_id`. The heavy lifting
(create def + embed examples, delete + cascade) lives in
:mod:`backend.embedding.authoring` so the REST and MCP surfaces share one path.

Graceful when no embedding model is configured: the intent + its examples are
still created (``embedding=None``); the classifier just won't match until
embeddings exist. See :mod:`backend.embedding.authoring`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id, require_account_id
from backend.embedding.authoring import (
    IntentAuthoringDuplicateError,
    IntentNotFoundError,
    build_account_embedder,
    create_intent_with_examples,
    delete_intent,
)
from backend.embedding.repository import IntentRepository

router = APIRouter()


class IntentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    description: str | None = None
    threshold: float | None = None


class IntentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    examples: list[str] = Field(default_factory=list)


@router.get("")
async def list_intents(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[IntentResponse]:
    """List intent definitions for this workspace + account."""
    repo = IntentRepository(session)
    rows = await repo.list_intents(workspace_id=workspace_id, account_id=account_id)
    return [
        IntentResponse(
            id=row.id,
            name=row.name,
            description=getattr(row, "description", None),
            threshold=getattr(row, "threshold", None),
        )
        for row in rows
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_intent(
    payload: IntentCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> IntentResponse:
    """Create an intent definition + its seed examples.

    Each example is embedded via the account's ``EmbeddingService`` and the
    vector stored on the example row. When no embedding model is configured the
    intent + examples are still created (``embedding=None``) — nothing is lost
    and the classifier won't match until embeddings exist. 409 on a duplicate
    name for the account.
    """
    embedder = await build_account_embedder(
        session, workspace_id=workspace_id, account_id=account_id
    )
    try:
        intent = await create_intent_with_examples(
            session,
            workspace_id=workspace_id,
            account_id=account_id,
            name=payload.name,
            threshold=payload.threshold,
            examples=payload.examples,
            embedder=embedder,
        )
    except IntentAuthoringDuplicateError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"an intent named {payload.name!r} already exists",
        ) from None
    await session.commit()
    return IntentResponse(
        id=intent.id,
        name=intent.name,
        description=intent.description or None,
        threshold=intent.threshold,
    )


@router.delete("/{intent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_intent_endpoint(
    intent_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """Delete an intent (its examples + vectors cascade). 404 if not found."""
    try:
        await delete_intent(
            session,
            intent_id=intent_id,
            workspace_id=workspace_id,
            account_id=account_id,
        )
    except IntentNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"intent {intent_id} not found",
        ) from None
    await session.commit()
