"""Knowledge retract / correct tools — UI-parity surface (Lift D3c).

Wraps the M3 retract / correct queue that the PWA's
:mod:`apps/pwa/components/knowledge/{Retract,Correct}Modal` modals drive
via ``/api/v1/inside/nodes/{node_ref}/{retract,correct}`` and the undo
companion at ``/api/v1/inside/corrections/{id}/undo``. Handlers are thin:
they re-use :class:`RetractionService` (the same application service the
REST endpoints call) so the MCP and PWA paths land on one canonical
mutation chain. Vault rooting mirrors
:mod:`backend.api.v1.decisions._helpers._vault_root` to keep MCP off the
forbidden :mod:`backend.api` subtree (importlinter contract D2).

Scopes follow the existing convention: ``mcp:write`` for retract / correct
/ undo (mutations).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel

from backend.config import get_settings
from backend.knowledge.application.retraction_service import RetractionService
from backend.knowledge.domain.retraction import (
    UNDO_WINDOW_SECONDS,
    OntologyAction,
)
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenWriter
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry


class _Envelope(RootModel[Any]):
    """Permissive output envelope — preserves the natural JSON shape."""


# ---------------------------------------------------------------------------
# Vault rooting — mirrors backend.api.v1.decisions._helpers._vault_root
# ---------------------------------------------------------------------------
def _vault_root(workspace_id: uuid.UUID) -> Path:
    settings = get_settings()
    return (
        Path(settings.knowledge_vault_root) / settings.knowledge_default_region / str(workspace_id)
    )


def _build_service(ctx: ToolContext) -> RetractionService:
    """Compose :class:`RetractionService` for the caller's vault.

    Tests inject a pre-built service into ``ctx.extras["retraction_service"]``
    so a unit run never touches the on-disk vault.
    """
    cached = ctx.extras.get("retraction_service") if ctx.extras else None
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    root = _vault_root(ctx.principal.workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    writer = GardenWriter(vault=Vault(root))
    return RetractionService(session=ctx.session, writer=writer)


async def _ensure_node_exists(workspace_id: uuid.UUID, node_ref: str) -> None:
    """Raise :class:`ToolError` unless ``node_ref`` exists in the caller's vault.

    Mirrors the REST endpoint's ``_ensure_node_exists`` guard so the MCP
    surface returns the same "not found" shape rather than persisting an
    orphan correction row. Path-traversal raises a distinct error.
    """
    root = _vault_root(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    storage = FileSystemStorage(root)
    try:
        exists = await storage.exists(node_ref)
    except ValueError as exc:  # path traversal
        raise ToolError(f"invalid node_ref: {node_ref}") from exc
    if not exists:
        raise ToolError(f"node not found: {node_ref}")


async def _issue_with_action(
    *,
    ctx: ToolContext,
    node_ref: str,
    action: OntologyAction,
    reason: str | None,
    correction_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Shared intake — verify, issue, commit, return the wire payload."""
    await _ensure_node_exists(ctx.principal.workspace_id, node_ref)
    service = _build_service(ctx)
    signal, outcome = await service.issue(
        workspace_id=ctx.principal.workspace_id,
        actor_id=ctx.principal.user_id,
        node_ref=node_ref,
        action=action,
        reason=reason,
        correction_id=correction_id,
    )
    await ctx.session.commit()
    return {
        "signal": signal.model_dump(mode="json"),
        "created": outcome == "created",
        "outcome": outcome,
        "undo_window_seconds": UNDO_WINDOW_SECONDS,
    }


# ---------------------------------------------------------------------------
# bsvibe_knowledge_retract
# ---------------------------------------------------------------------------
class RetractInput(BaseModel):
    """Mirror of :class:`RetractRequest` (REST) + the path arg.

    ``node_ref`` is the vault-relative POSIX path (e.g.
    ``garden/seedling/foo.md``) — the same id the PWA modal carries.
    ``correction_id`` is optional; clients that want safe-retries supply
    a UUID they generated so a re-issue with the same id is a no-op (the
    server returns the persisted signal + ``created=False``).
    """

    model_config = ConfigDict(extra="forbid")

    node_ref: str = Field(min_length=1, max_length=512)
    reason: str | None = Field(default=None, max_length=280)
    correction_id: uuid.UUID | None = None


async def _h_retract(args: RetractInput, ctx: ToolContext) -> Any:
    return await _issue_with_action(
        ctx=ctx,
        node_ref=args.node_ref,
        action="retract",
        reason=args.reason,
        correction_id=args.correction_id,
    )


# ---------------------------------------------------------------------------
# bsvibe_knowledge_correct
# ---------------------------------------------------------------------------
class CorrectInput(BaseModel):
    """Mirror of :class:`CorrectRequest` (REST) + the path arg.

    Preserved for wire-compatibility, but the tool currently refuses with an
    "unavailable" :class:`ToolError`: the in-place field-rewrite editor was
    never built, so confirming a correction would be a false success.
    """

    model_config = ConfigDict(extra="forbid")

    node_ref: str = Field(min_length=1, max_length=512)
    corrections: dict[str, str] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=280)
    correction_id: uuid.UUID | None = None


async def _h_correct(args: CorrectInput, ctx: ToolContext) -> Any:
    # The field-rewrite editor was never built. Issuing a correction would
    # persist a row + later emit a false ``ontology.correction.applied`` audit
    # for an operation that mutates nothing. Refuse honestly instead.
    raise ToolError("correction (in-place field rewrite) is not available yet")


# ---------------------------------------------------------------------------
# bsvibe_knowledge_undo_correction
# ---------------------------------------------------------------------------
class UndoCorrectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correction_id: uuid.UUID


class UndoCorrectionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correction_id: str
    status: str


async def _h_undo(args: UndoCorrectionInput, ctx: ToolContext) -> Any:
    service = _build_service(ctx)
    result = await service.undo(
        correction_id=args.correction_id,
        workspace_id=ctx.principal.workspace_id,
    )
    if result == "not_found":
        raise ToolError(f"correction not found: {args.correction_id}")
    await ctx.session.commit()
    return UndoCorrectionOutput(correction_id=str(args.correction_id), status=result)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_knowledge_retraction_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_knowledge_retract",
            description=(
                "Open a retract for a garden note. Queued — the tombstone is "
                "committed when the 30s undo window closes (or sooner if a "
                "subsequent call to `bsvibe_knowledge_undo_correction` cancels "
                "it). Idempotent on `correction_id`. Mirrors the PWA "
                "InspectorActions retract modal."
            ),
            input_schema=RetractInput,
            output_schema=_Envelope,
            handler=_h_retract,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.knowledge_retract.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_knowledge_correct",
            description=(
                "NOT AVAILABLE YET. The in-place field-rewrite editor for garden "
                "notes was never built, so this tool refuses with an 'unavailable' "
                "error rather than confirm a correction that changes nothing. Use "
                "`bsvibe_knowledge_retract` to remove a note from future runs."
            ),
            input_schema=CorrectInput,
            output_schema=_Envelope,
            handler=_h_correct,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.knowledge_correct.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_knowledge_undo_correction",
            description=(
                "Undo a queued retract / correct inside the 30s window. Returns "
                "the terminal status (`undone` / `expired` / `already_applied` / "
                "`already_undone`). Idempotent."
            ),
            input_schema=UndoCorrectionInput,
            output_schema=UndoCorrectionOutput,
            handler=_h_undo,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.knowledge_undo_correction.invoked",
        )
    )


__all__ = ["register_knowledge_retraction_tools"]
