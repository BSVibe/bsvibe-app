"""/api/v1/messages — founder-direct submission into the workflow.

The Direct path entrypoint (Workflow §11.1). A founder types a request and
we land it on the workflow exactly as an inbound webhook would, via
:class:`backend.intake.direct.DirectTrigger` (``source="direct"``). The
agent workers (intake → agent → delivery) drive it the rest of the way to
a delivered artifact — this surface only accepts the trigger.

Write-only POST: the run is *created* by the worker pipeline, never by the
HTTP request, so this returns an acceptance receipt, not a run.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user_row, get_db_session, get_workspace_id
from backend.config import get_settings
from backend.identity.db import UserRow
from backend.intake.direct import DirectTrigger
from backend.workers.emit import STREAM_INTAKE, emit_stream_notification, get_emit_redis_client

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
    """
    trigger = DirectTrigger(session)
    outcome = await trigger.submit(
        workspace_id=workspace_id,
        founder_id=user_row.id,
        text=body.text,
        product_id=body.product_id,
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


__all__ = ["MessageAccepted", "MessageCreate", "router"]
