"""Decision tools — UI-parity workflow surface (Lift D3b).

Wraps the founder-approval queue that the PWA's ``Decisions`` view drives
via ``/api/v1/decisions``. Vault-FS-as-SoT (no DB table) — every read /
write lands on the per-workspace
``<knowledge_vault_root>/<region>/<workspace_id>/`` directory the
:mod:`backend.knowledge.canonicalization` pipeline owns, addressed exactly
the same way the REST surface does so a listed ``proposal_id`` round-trips
back into accept / reject.

Read tools wrap :meth:`InMemoryCanonicalizationIndex.list_proposals` /
``list_decisions``; write tools wrap
:meth:`CanonicalizationService.accept_proposal` / ``reject_proposal``.
Storage rooting is replicated from
:mod:`backend.api.v1.decisions._helpers` to keep MCP off the forbidden
:mod:`backend.api` subtree (importlinter contract D2).

Scopes follow the existing convention: ``mcp:read`` for list / show,
``mcp:write`` for accept / reject (irreversible mutations land on the
same scope :mod:`safe_mode_tools` uses).
"""

from __future__ import annotations

import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel

from backend.config import get_settings
from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.index import (
    InMemoryCanonicalizationIndex,
    _is_canon_proposal_path,
)
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage
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


async def _build_index(ctx: ToolContext) -> InMemoryCanonicalizationIndex:
    """Return a fresh per-workspace vault index.

    Tests inject a pre-built index into ``ctx.extras["canon_index"]`` so a
    unit run never touches the on-disk vault.
    """
    cached = ctx.extras.get("canon_index") if ctx.extras else None
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    root = _vault_root(ctx.principal.workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    index = InMemoryCanonicalizationIndex()
    await index.initialize(FileSystemStorage(root))
    return index


async def _build_service(ctx: ToolContext) -> CanonicalizationService:
    cached = ctx.extras.get("canon_service") if ctx.extras else None
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    root = _vault_root(ctx.principal.workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    storage = FileSystemStorage(root)
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    return CanonicalizationService(
        store=NoteStore(storage),
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
    )


def _action_handle(proposal: models.ProposalEntry) -> tuple[str, str]:
    """``(action_kind, action_path)`` of a proposal's first draft."""
    for draft in proposal.action_drafts:
        parts = PurePosixPath(draft).parts
        if len(parts) >= 2 and parts[0] == "actions":
            return parts[1], draft
    return proposal.kind, proposal.path


def _proposal_to_dict(proposal: models.ProposalEntry) -> dict[str, Any]:
    action_kind, action_path = _action_handle(proposal)
    return {
        "id": proposal.path,
        "proposal_kind": proposal.kind,
        "action_kind": action_kind,
        "action_path": action_path,
        "status": proposal.status,
        "score": proposal.proposal_score,
        "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
        "expires_at": proposal.expires_at.isoformat() if proposal.expires_at else None,
        "strategy": proposal.strategy,
        "generator": proposal.generator,
        "generator_version": proposal.generator_version,
        "evidence": proposal.evidence,
        "affected_paths": proposal.affected_paths,
        "action_drafts": proposal.action_drafts,
    }


def _decision_to_dict(decision: models.DecisionEntry) -> dict[str, Any]:
    return {
        "id": decision.path,
        "decision_kind": decision.kind,
        "status": decision.status,
        "maturity": decision.maturity,
        "created_at": decision.created_at.isoformat() if decision.created_at else None,
    }


def _ensure_proposal_path(proposal_id: str) -> None:
    if not _is_canon_proposal_path(proposal_id):
        raise ToolError(f"proposal not found: {proposal_id}")


# ---------------------------------------------------------------------------
# bsvibe_decisions_list
# ---------------------------------------------------------------------------
class DecisionsListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


async def _h_list(args: DecisionsListInput, ctx: ToolContext) -> Any:
    index = await _build_index(ctx)
    proposals = await index.list_proposals(status=args.status)
    proposals.sort(key=lambda p: p.created_at, reverse=True)
    return _Envelope([_proposal_to_dict(p) for p in proposals[: args.limit]])


# ---------------------------------------------------------------------------
# bsvibe_decisions_show
# ---------------------------------------------------------------------------
class DecisionsShowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str = Field(min_length=1, max_length=1024)


async def _h_show(args: DecisionsShowInput, ctx: ToolContext) -> Any:
    _ensure_proposal_path(args.decision_id)
    index = await _build_index(ctx)
    proposals = await index.list_proposals()
    match = next((p for p in proposals if p.path == args.decision_id), None)
    if match is None:
        raise ToolError(f"proposal not found: {args.decision_id}")
    return _Envelope(_proposal_to_dict(match))


# ---------------------------------------------------------------------------
# bsvibe_decisions_log
# ---------------------------------------------------------------------------
class DecisionsLogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=50, ge=1, le=200)


async def _h_log(args: DecisionsLogInput, ctx: ToolContext) -> Any:
    index = await _build_index(ctx)
    decisions = await index.list_decisions()
    decisions.sort(key=lambda d: d.created_at, reverse=True)
    return _Envelope([_decision_to_dict(d) for d in decisions[: args.limit]])


# ---------------------------------------------------------------------------
# bsvibe_decisions_resolve — accept | reject in one tool, action discriminator
# ---------------------------------------------------------------------------
class DecisionsResolveInput(BaseModel):
    """``action`` is ``accept`` (apply linked drafts) or ``reject``.

    ``comment`` is the rejection reason; ignored on accept.
    """

    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1, max_length=1024)
    action: str
    comment: str | None = None


async def _h_resolve(args: DecisionsResolveInput, ctx: ToolContext) -> Any:
    if args.action not in ("accept", "reject"):
        raise ToolError(f"action must be 'accept' or 'reject', got {args.action!r}")
    _ensure_proposal_path(args.decision_id)
    service = await _build_service(ctx)
    actor = str(ctx.principal.user_id)
    if args.action == "accept":
        try:
            results = await service.accept_proposal(args.decision_id, actor=actor)
        except FileNotFoundError as exc:
            raise ToolError(f"proposal not found: {args.decision_id}") from exc
        except ValueError as exc:
            raise ToolError(f"proposal not resolvable: {exc}") from exc
        proposal_status = (
            "accepted"
            if results and all(r.final_status == "applied" for r in results)
            else "pending"
        )
        return _Envelope(
            {
                "proposal_path": args.decision_id,
                "status": proposal_status,
                "results": [
                    {
                        "action_path": r.action_path,
                        "final_status": r.final_status,
                        "affected_paths": list(r.affected_paths),
                        "error": r.error,
                    }
                    for r in results
                ],
            }
        )
    # reject
    try:
        await service.reject_proposal(args.decision_id, actor=actor, reason=args.comment)
    except FileNotFoundError as exc:
        raise ToolError(f"proposal not found: {args.decision_id}") from exc
    except ValueError as exc:
        raise ToolError(f"proposal not resolvable: {exc}") from exc
    return _Envelope(
        {
            "proposal_path": args.decision_id,
            "status": "rejected",
            "reason": args.comment,
        }
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_decisions_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_decisions_list",
            description=(
                "List canonicalization proposals (founder-approval queue) in the "
                "active workspace, newest first. Pass `status='pending'` to scope "
                "to just the queue; omit for every status."
            ),
            input_schema=DecisionsListInput,
            output_schema=_Envelope,
            handler=_h_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_decisions_show",
            description=(
                "Show one proposal by id (its vault path). Carries the full "
                "evidence, generator, and linked action drafts."
            ),
            input_schema=DecisionsShowInput,
            output_schema=_Envelope,
            handler=_h_show,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_decisions_log",
            description=(
                "List resolved decision-memory notes (the founder-approval audit "
                "trail) in the active workspace, newest first."
            ),
            input_schema=DecisionsLogInput,
            output_schema=_Envelope,
            handler=_h_log,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_decisions_resolve",
            description=(
                "Resolve a queued proposal. `action='accept'` applies every "
                "linked typed action (the same code path the PWA accept button "
                "drives); `action='reject'` records the decision with an "
                "optional `comment` and leaves drafts untouched."
            ),
            input_schema=DecisionsResolveInput,
            output_schema=_Envelope,
            handler=_h_resolve,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.decisions_resolve.invoked",
        )
    )


__all__ = ["register_decisions_tools"]
