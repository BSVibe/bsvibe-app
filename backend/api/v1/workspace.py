"""``/api/v1/workspace`` — read + rename the caller's workspace.

Sits alongside :mod:`backend.api.v1.workspace_compliance` under the singular
``/workspace`` prefix. Where the compliance routes export and document the
workspace (Art. 15 / 20 / 30), these routes are the everyday founder-facing
surface: "what's my workspace called, and let me rename it."

Two endpoints:

* ``GET    /api/v1/workspace`` — returns the active workspace's id + name.
  Drives Settings → General's "Workspace name" field so it no longer falls
  back to the founder's email when no real name is stored
  (the /impeccable audit's Lift 13 finding).
* ``PATCH  /api/v1/workspace`` — accepts ``{ name }`` and stores it on the
  active workspace's row. ``extra="forbid"`` rejects unknown fields.

Workspace resolution + RLS guard fire automatically via
``Depends(get_workspace_id)`` exactly the same way the compliance routes
engage them.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.v1._identity_deps import get_workspace_repository
from backend.identity.domain.repositories import WorkspaceRepository

router = APIRouter()


class WorkspaceOut(BaseModel):
    """GET response — the basic workspace facts the PWA surfaces."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    # Lift Q1 — per-workspace audit_outbox retention knob. ``None`` =
    # forever (roadmap §6 결정 로그 Q1 default), ``N >= 1`` = the daily
    # retention sweep rotates ``audit_outbox`` rows past N days.
    audit_retention_days: int | None = None
    # Lift E1 — workspace-default ModelAccount for the new
    # :class:`backend.dispatch.resolver.ModelAccountResolver`. ``None`` =
    # the founder has not picked one yet; the resolver will raise
    # ``NoMatchingRouteError`` when no rule matches and this is unset
    # (BSVibe NEVER auto-stamps it per ``bsvibe-no-implicit-routing``).
    default_account_id: uuid.UUID | None = None
    # The language LLM-generated user-facing prose is written in (knowledge
    # notes, decision questions, framing). A short locale tag; "en" default.
    language: str = "en"
    # L3 (#5) — Safe Mode. ``True`` (Safe): every shipped deliverable is held
    # in the Safe Mode queue for founder approval. ``False`` (Auto): deliverables
    # auto-dispatch — the delivery gate is bypassed (the Claude-Code
    # "bypass permissions" UX). Real blocks (ask_user_question / verification
    # failures) still surface as Decisions regardless of this flag.
    safe_mode: bool = True


class WorkspaceUpdate(BaseModel):
    """PATCH body — the editable workspace surface. ``extra="forbid"``
    rejects unknown fields so writes can't quietly mutate columns the
    route doesn't own.

    ``name`` is optional so a PATCH can target ``audit_retention_days``
    alone (and vice-versa); an empty PATCH (``{}``) is a no-op, which
    matches the standard PATCH semantics. Pydantic's ``model_fields_set``
    is what tells the handler which fields the caller actually sent vs.
    left absent — important for ``audit_retention_days`` where
    explicit-``null`` (unset to forever) is a distinct intent from
    "field absent" (leave it alone)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    # Lift Q1 — per-workspace audit_outbox retention knob. ``ge=1``
    # enforces the "N >= 1 days" half of the column's contract; a caller
    # sending ``null`` (the OTHER valid value, = forever) clears the
    # column. The "no change" case is "field absent from PATCH body" —
    # not the same as ``null`` (use ``model_fields_set`` to distinguish).
    audit_retention_days: int | None = Field(default=None, ge=1)
    # Lift E1 — workspace-default ModelAccount fallback for the new
    # :class:`backend.dispatch.resolver.ModelAccountResolver`. Same PATCH
    # semantics as ``audit_retention_days``: omit to leave unchanged,
    # send ``null`` to UNSET (= no fallback, resolver hard-fails on
    # unmatched rules), send a UUID to set. The handler validates that
    # the target account exists in this workspace + is active — a
    # cross-workspace pointer is a 422.
    default_account_id: uuid.UUID | None = Field(default=None)
    # The LLM output language. Omit to leave unchanged; a supported tag
    # ("en" / "ko") to set. The PWA Language control sends this alongside the
    # client locale so the UI and the generated prose share one language.
    language: Literal["en", "ko"] | None = Field(default=None)
    # L3 (#5) — Safe Mode toggle. Omit to leave unchanged; ``True`` (Safe) holds
    # deliverables for approval, ``False`` (Auto) auto-dispatches. The founder
    # flips it from Settings → General (or the MCP set-safe-mode tool).
    safe_mode: bool | None = Field(default=None)


@router.get("", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    workspaces: Annotated[WorkspaceRepository, Depends(get_workspace_repository)],
) -> WorkspaceOut:
    """Return the active workspace's id + name + audit retention."""
    workspace = await workspaces.get(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return WorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        audit_retention_days=workspace.audit_retention_days,
        default_account_id=workspace.default_account_id,
        language=workspace.language,
        safe_mode=workspace.safe_mode,
    )


@router.patch("", response_model=WorkspaceOut)
async def update_workspace(
    payload: WorkspaceUpdate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    workspaces: Annotated[WorkspaceRepository, Depends(get_workspace_repository)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkspaceOut:
    """Update the workspace name and/or audit_retention_days.

    The workspace_id contextvar is what selects the row — the caller cannot
    write a different workspace's row, defense-in-depth from the RLS GUC on
    the same connection.

    PATCH semantics: only fields PRESENT in the request body are written;
    omitted fields are left untouched. For ``audit_retention_days``,
    explicit ``null`` (in the body) UNSETS to forever; absence leaves the
    existing value alone. ``model_fields_set`` is what distinguishes the
    two cases.
    """
    workspace = await workspaces.get(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    sent = payload.model_fields_set
    if "name" in sent:
        # ``min_length=1`` already rejected the empty string; we only
        # have to strip whitespace.
        assert payload.name is not None  # noqa: S101 — guarded by min_length=1
        workspace.name = payload.name.strip()
    if "audit_retention_days" in sent:
        workspace.audit_retention_days = payload.audit_retention_days
    if "default_account_id" in sent:
        # Validate the target is in this workspace + active — a stale or
        # cross-workspace pointer would be a silent-wrong-route bug
        # later. ``null`` UNSETs (= no fallback).
        if payload.default_account_id is not None:
            await _ensure_account_in_workspace(session, workspace_id, payload.default_account_id)
        workspace.default_account_id = payload.default_account_id
    if "language" in sent and payload.language is not None:
        workspace.language = payload.language
    if "safe_mode" in sent and payload.safe_mode is not None:
        workspace.safe_mode = payload.safe_mode
    await session.commit()
    return WorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        audit_retention_days=workspace.audit_retention_days,
        default_account_id=workspace.default_account_id,
        language=workspace.language,
        safe_mode=workspace.safe_mode,
    )


async def _ensure_account_in_workspace(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Reject the PATCH if the target ModelAccount isn't active in this workspace."""
    from backend.router.infrastructure.repositories import (  # noqa: PLC0415
        SqlAlchemyModelAccountRepository,
    )

    repo = SqlAlchemyModelAccountRepository(session)
    accounts = await repo.list_active_for_workspace(workspace_id=workspace_id)
    if not any(a.id == account_id for a in accounts):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=("default_account_id must reference an active ModelAccount in this workspace"),
        )


__all__ = ["router"]
