"""Intent authoring tools — UI-parity surface (NL-native routing Lift N2).

Mirrors the REST surface at ``/api/v1/intents`` (see
:mod:`backend.api.v1.intents`). Intent definitions are the SEMANTIC categories
the N1 classifier matches incoming work against — the founder names a category
("marketing", "design", "complex-coding") + a few example phrases, which get
embedded so run-routing rules can key on the NATURE of the work
(``classified_intent``), not just the fixed execution-stage callers.

Handlers delegate to the SAME :mod:`backend.embedding.authoring` service the
REST path uses, so both surfaces land on one create/delete + embedding contract.
Intents are scoped to the workspace's personal account (resolved via
:func:`ensure_personal_account`), matching the REST endpoint's
:func:`require_account_id` axis. Graceful when no embedding model is
configured — the intent + examples are still created with ``embedding=None``.

Scopes follow the existing convention: ``mcp:read`` for list, ``mcp:write`` for
create / delete.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel

from backend.embedding.authoring import (
    IntentAuthoringDuplicateError,
    IntentNotFoundError,
    build_account_embedder,
    create_intent_with_examples,
    delete_intent,
)
from backend.embedding.repository import IntentRepository
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.router.accounts.account_service import ensure_personal_account


class _Envelope(RootModel[Any]):
    """Permissive output envelope — preserves the natural JSON shape."""


async def _account_id(ctx: ToolContext) -> uuid.UUID:
    """Resolve the workspace's personal account (create-on-read), matching the
    REST endpoint's ``require_account_id`` axis."""
    account = await ensure_personal_account(ctx.session, workspace_id=ctx.principal.workspace_id)
    return account.id


# ---------------------------------------------------------------------------
# bsvibe_intents_list
# ---------------------------------------------------------------------------
class IntentsListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _h_list(_args: IntentsListInput, ctx: ToolContext) -> Any:
    account_id = await _account_id(ctx)
    repo = IntentRepository(ctx.session)
    rows = await repo.list_intents(workspace_id=ctx.principal.workspace_id, account_id=account_id)
    return _Envelope(
        [
            {
                "id": str(r.id),
                "name": r.name,
                "description": r.description or None,
                "threshold": r.threshold,
            }
            for r in rows
        ]
    )


# ---------------------------------------------------------------------------
# bsvibe_intents_create
# ---------------------------------------------------------------------------
class IntentsCreateInput(BaseModel):
    """Mirror of :class:`backend.api.v1.intents.IntentCreate`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    examples: list[str] = Field(default_factory=list)


async def _h_create(args: IntentsCreateInput, ctx: ToolContext) -> Any:
    account_id = await _account_id(ctx)
    embedder = await build_account_embedder(
        ctx.session, workspace_id=ctx.principal.workspace_id, account_id=account_id
    )
    try:
        intent = await create_intent_with_examples(
            ctx.session,
            workspace_id=ctx.principal.workspace_id,
            account_id=account_id,
            name=args.name,
            threshold=args.threshold,
            examples=args.examples,
            embedder=embedder,
        )
    except IntentAuthoringDuplicateError as exc:
        await ctx.session.rollback()
        raise ToolError(f"an intent named {args.name!r} already exists") from exc
    await ctx.session.commit()
    return _Envelope(
        {
            "id": str(intent.id),
            "name": intent.name,
            "description": intent.description or None,
            "threshold": intent.threshold,
        }
    )


# ---------------------------------------------------------------------------
# bsvibe_intents_delete
# ---------------------------------------------------------------------------
class IntentsDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: uuid.UUID


class IntentsDeleteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deleted: bool
    intent_id: str


async def _h_delete(args: IntentsDeleteInput, ctx: ToolContext) -> Any:
    account_id = await _account_id(ctx)
    try:
        await delete_intent(
            ctx.session,
            intent_id=args.intent_id,
            workspace_id=ctx.principal.workspace_id,
            account_id=account_id,
        )
    except IntentNotFoundError as exc:
        raise ToolError(f"intent not found: {args.intent_id}") from exc
    await ctx.session.commit()
    return IntentsDeleteOutput(deleted=True, intent_id=str(args.intent_id))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_intents_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_intents_list",
            description=(
                "List intent definitions for the active workspace's personal "
                "account. Intents are the SEMANTIC categories the routing "
                "classifier matches work against (classified_intent) — e.g. "
                "marketing / design / complex-coding. Mirrors GET /api/v1/intents."
            ),
            input_schema=IntentsListInput,
            output_schema=_Envelope,
            handler=_h_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_intents_create",
            description=(
                "Create an intent definition — a semantic routing category the "
                "classifier can match work against. Mirrors POST /api/v1/intents: "
                "name + optional threshold (default 0.65) + a few example phrases. "
                "The examples are embedded so run-routing rules keyed on "
                "classified_intent (e.g. classified_intent == marketing -> sonnet) "
                "can match incoming work. Graceful when no embedding model is "
                "configured — the intent + examples are still created and will be "
                "embedded once a model is set."
            ),
            input_schema=IntentsCreateInput,
            output_schema=_Envelope,
            handler=_h_create,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.intents_create.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_intents_delete",
            description=(
                "Delete an intent definition by id (its examples + embeddings "
                "cascade). Mirrors DELETE /api/v1/intents/{id}."
            ),
            input_schema=IntentsDeleteInput,
            output_schema=IntentsDeleteOutput,
            handler=_h_delete,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.intents_delete.invoked",
        )
    )


__all__ = ["register_intents_tools"]
