"""/api/v1/presets — list built-in presets + apply.

Apply path:

    POST /api/v1/presets/{name}/apply
    body: { "model_mapping": { "economy": "...", "balanced": "...", "premium": "..." } }

Wires :class:`backend.gateway.presets.PresetService.apply_preset` against
the current (workspace, account). EmbeddingService is wired when present
(intent examples get embedded inline); when not configured, examples land
unembedded and the operator can run a re-embed pass later.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id, require_account_id
from backend.gateway.presets.models import ModelMapping
from backend.gateway.presets.registry import PresetRegistry
from backend.gateway.presets.service import AuditEmitterProtocol, PresetService
from backend.supervisor.audit.events import AuditEventBase

router = APIRouter()


class _NoopAuditEmitter:
    """Default audit sink — drops the event.

    Production wiring (Bundle G) injects :class:`AuditEmitter` against
    the request-scoped session so each preset apply lands a row in the
    audit outbox. Tests use this no-op to skip the audit table.
    """

    async def emit(self, event: AuditEventBase, *, session: AsyncSession) -> None:
        del event, session


class PresetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    intent_count: int
    rule_count: int


class PresetApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_mapping: ModelMapping


class PresetApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_name: str
    rules_created: int
    intents_created: int
    examples_created: int


@router.get("")
async def list_presets() -> list[PresetSummary]:
    return [
        PresetSummary(
            name=p.name,
            description=p.description,
            intent_count=len(p.intents),
            rule_count=len(p.rules),
        )
        for p in PresetRegistry().list_all()
    ]


@router.post("/{preset_name}/apply", status_code=status.HTTP_200_OK)
async def apply_preset(
    preset_name: str,
    body: PresetApplyBody,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PresetApplyResponse:
    emitter: AuditEmitterProtocol = _NoopAuditEmitter()
    service = PresetService(
        session=session,
        audit_emitter=emitter,
        actor_id=str(workspace_id),
    )
    try:
        result = await service.apply_preset(
            workspace_id=workspace_id,
            account_id=account_id,
            preset_name=preset_name,
            model_mapping=body.model_mapping,
            embedding_service=None,  # caller can wire post-Bundle G
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await session.commit()
    return PresetApplyResponse(
        preset_name=result.preset_name,
        rules_created=result.rules_created,
        intents_created=result.intents_created,
        examples_created=result.examples_created,
    )
