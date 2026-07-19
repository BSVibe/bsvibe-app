"""Checkpoint tools — the founder judges a run-blocking Decision over MCP (C2).

Loop link #5 ("the founder judges"). When a run pauses on a Decision — the work
LLM called ``ask_user_question``, or an executor run did not verify
(``verification_failed`` / ``human_review_required``) — it awaits a human answer.
C1 already lets the PWA resolve it via ``POST /api/v1/checkpoints/{id}/resolve``;
C2 gives an *away* founder (or an agent acting for them) the SAME re-entry point
from any MCP client, so a run can be unblocked without opening the PWA.

Two tools, mirroring the REST ``/api/v1/checkpoints`` surface:

* ``bsvibe_checkpoints_list_pending`` (``mcp:read``) — the pending run-blocking
  Decisions awaiting the founder, built from the SAME
  :mod:`~backend.workflow.application._checkpoint_shared` kind → question /
  options / actions helpers the REST list uses.
* ``bsvibe_checkpoints_resolve`` (``mcp:write``) — a thin transport over the C1
  :func:`~backend.workflow.application.checkpoint_resolution.resolve_checkpoint`
  service (no duplicated resolve logic): record the founder's answer, fold it
  into the run payload, settle it as knowledge, and resume the run RUNNING →
  OPEN so ``AgentWorker.drive_once`` re-picks it.

**``ship`` is PWA-only (excluded from MCP).** A ``ship`` force-merges the run's
version onto ``main`` overriding a failed verification — a high-consequence,
irreversible act the founder should perform deliberately from the full PWA
review surface, not fire from a chat client. The C1 service still supports
``ship`` (the REST/PWA path uses it); the gate lives HERE, in the tool layer:
``bsvibe_checkpoints_resolve`` rejects ``action_key="ship"`` with a clear
:class:`~backend.mcp.api.ToolError`, and the list tool omits ``ship`` from each
Decision's offered actions so it is never surfaced as an MCP-actionable option.
``retry`` and ``discard`` remain available over MCP.

Named ``bsvibe_checkpoints_*`` (mirrors the REST path) — NOT
``bsvibe_decisions_*``, which is already taken by the KNOWLEDGE canonicalization
surface (:mod:`~backend.mcp.tools.decisions_tools`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.workflow.application._checkpoint_shared import (
    ACTION_SHIP,
    DecisionAction,
    _decision_actions,
    _decision_options,
    _question_text,
)
from backend.workflow.application.checkpoint_resolution import (
    CheckpointNotFound,
    InvalidAction,
    ProductWorkspaceBusy,
    ProductWorkspaceMergeFailed,
    resolve_checkpoint,
)
from backend.workflow.infrastructure.db import Decision, DecisionStatus, RunStatus
from backend.workflow.infrastructure.repositories import SqlAlchemyDecisionRepository

#: The one action the founder may take ONLY from the PWA. MCP callers get a
#: ToolError instead of a force-merge onto main. Kept as a named alias so the
#: gate and the C1 service stay pinned to the same wire identifier.
_PWA_ONLY_ACTION = ACTION_SHIP

_SHIP_REJECTION = "ship (force-merge to main) is available only in the PWA"


async def _workspace_language(ctx: ToolContext) -> str:
    """The workspace OUTPUT language (``ko`` / ``en``) for founder-facing text.

    Mirrors :func:`backend.api.deps.get_output_language` — the ``ask_user_question``
    question is already generated in the founder's language, but the fixed
    executor-Decision "needs you" lines are localized here by
    ``workspaces.language``. Best-effort: a missing row / read hiccup degrades to
    ``en`` rather than failing the tool.
    """
    try:
        lang = (
            await ctx.session.execute(
                select(WorkspaceRow.language).where(WorkspaceRow.id == ctx.principal.workspace_id)
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — language is best-effort; never fail the tool
        return "en"
    return lang or "en"


# ---------------------------------------------------------------------------
# bsvibe_checkpoints_list_pending — the founder's inbox of paused runs.
# ---------------------------------------------------------------------------
class CheckpointsListPendingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckpointItem(BaseModel):
    """One pending run-blocking Decision — mirrors REST ``CheckpointResponse``.

    ``actions`` reflects the MCP ship-gate: the PWA-only ``ship`` action is
    filtered out so an MCP client is never offered an action it cannot invoke
    (``retry`` / ``discard`` remain). ``None`` for a vanilla ``ask_user_question``
    Decision (those resolve via ``options`` + free-text ``answer`` only).
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    kind: str
    question: str
    context: str | None = None
    options: list[str] | None = None
    actions: list[DecisionAction] | None = None
    rationale: str | None = None
    created_at: datetime


class CheckpointsListPendingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    items: list[CheckpointItem]


def _payload_context(decision: Decision) -> str | None:
    """Extra founder-facing context the Decision mint recorded on its payload,
    if any (a non-empty string) — else ``None``."""
    payload = decision.payload or {}
    if not isinstance(payload, dict):
        return None
    value = payload.get("context")
    return value if isinstance(value, str) and value.strip() else None


def _mcp_actions(decision: Decision) -> list[DecisionAction] | None:
    """The one-click actions offered over MCP for ``decision`` — the C1 action
    set MINUS the PWA-only ``ship`` (so ``verification_failed`` surfaces
    ``retry`` / ``discard`` here, never ``ship``). ``None`` when the kind carries
    no actions at all."""
    actions = _decision_actions(decision)
    if actions is None:
        return None
    offered = [a for a in actions if a.key != _PWA_ONLY_ACTION]
    return offered or None


async def _h_list_pending(_args: CheckpointsListPendingInput, ctx: ToolContext) -> Any:
    language = await _workspace_language(ctx)
    decisions = SqlAlchemyDecisionRepository(ctx.session)
    rows = await decisions.list_pending_by_workspace(ctx.principal.workspace_id)
    items = [
        CheckpointItem(
            id=row.id,
            run_id=row.run_id,
            kind=row.decision,
            question=_question_text(row, language),
            context=_payload_context(row),
            options=_decision_options(row),
            actions=_mcp_actions(row),
            rationale=row.rationale,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return CheckpointsListPendingOutput(total=len(items), items=items)


# ---------------------------------------------------------------------------
# bsvibe_checkpoints_resolve — record the answer + resume the run.
# ---------------------------------------------------------------------------
class CheckpointResolveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_id: uuid.UUID
    # Free-text answer for an ``ask_user_question`` Decision (or an "Other"
    # answer off the offered options). Optional so an action-only resolve
    # (``retry`` / ``discard``) can omit it.
    answer: str = Field(default="", max_length=8000)
    # One-click action for an executor B2b Decision — ``retry`` or ``discard``
    # over MCP. ``ship`` is rejected (PWA-only).
    action_key: str | None = None
    # Optional "why I'm discarding" captured as reusable negative knowledge on a
    # ``discard``; ignored otherwise.
    reason: str = Field(default="", max_length=2000)


class CheckpointResolveOutput(BaseModel):
    """Mirrors REST ``ResolveResponse``."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    status: DecisionStatus
    resolution: str
    resolved_at: datetime
    run_status: RunStatus


async def _h_resolve(args: CheckpointResolveInput, ctx: ToolContext) -> Any:
    # The ship-gate — rejected BEFORE any side effect, so the Decision stays
    # pending and the run is untouched. The C1 service still supports ship (the
    # PWA path); MCP simply refuses to be the surface that force-merges to main.
    if args.action_key == _PWA_ONLY_ACTION:
        raise ToolError(_SHIP_REJECTION)

    language = await _workspace_language(ctx)
    try:
        outcome = await resolve_checkpoint(
            ctx.session,
            workspace_id=ctx.principal.workspace_id,
            checkpoint_id=args.checkpoint_id,
            answer=args.answer,
            action_key=args.action_key,
            reason=args.reason,
            actor_id=ctx.principal.user_id,
            language=language,
        )
    except (CheckpointNotFound, InvalidAction) as exc:
        # The service flushed but did not commit; roll back the aborted attempt
        # so nothing partial leaks into the next tool call on this session.
        await ctx.session.rollback()
        raise ToolError(str(exc)) from exc
    except (ProductWorkspaceBusy, ProductWorkspaceMergeFailed) as exc:
        # Reachable only via ``ship`` — which MCP already rejects above — so this
        # is defence-in-depth: surface it as a plain ToolError rather than leak
        # the git-merge internals.
        await ctx.session.rollback()
        raise ToolError(str(exc)) from exc

    # C1 owns no transaction boundary — the caller commits (mirrors the REST
    # handler + safe_mode / schedule tools).
    await ctx.session.commit()

    return CheckpointResolveOutput(
        id=outcome.decision_id,
        run_id=outcome.run_id,
        status=outcome.status,
        resolution=outcome.resolution,
        resolved_at=outcome.resolved_at,
        run_status=outcome.run_status,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_checkpoint_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_checkpoints_list_pending",
            description=(
                "List run-blocking Decisions (checkpoints) awaiting the founder "
                "in the active workspace, newest first. Each carries the question "
                "and its options + one-click actions. Actions exclude `ship` "
                "(force-merge to main is PWA-only); `retry` / `discard` remain."
            ),
            input_schema=CheckpointsListPendingInput,
            output_schema=CheckpointsListPendingOutput,
            handler=_h_list_pending,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_checkpoints_resolve",
            description=(
                "Resolve one pending run-blocking Decision and resume the paused "
                "run. Give `answer` (free text) for an ask_user_question, or "
                "`action_key` in {retry, discard} for a verification_failed / "
                'human_review_required Decision. `action_key="ship"` is rejected '
                "— force-merge to main is available only in the PWA."
            ),
            input_schema=CheckpointResolveInput,
            output_schema=CheckpointResolveOutput,
            handler=_h_resolve,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.checkpoints_resolve.invoked",
        )
    )


__all__ = ["register_checkpoint_tools"]
