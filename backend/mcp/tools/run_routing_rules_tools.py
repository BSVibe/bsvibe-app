"""Run-routing rule tools — UI-parity workflow surface (Lift E7).

Mirrors the REST surface at ``/api/v1/run-routing`` (see
:mod:`backend.api.v1.run_routing`). These rules pick WHICH ModelAccount
handles a run, keyed on the dispatch ``caller_id`` + the run's framed
signals — distinct from the legacy model-routing rules
(``bsvibe_routing_rules_*``) which pick the LLM model within a run via
the litellm hook.

The lift exists because the dogfood (qazasa123) surfaced that the new
run-routing system lived behind REST only, while the legacy
``bsvibe_routing_rules_*`` tools route through a different engine + a
different (heuristic) ALLOWED_FIELDS whitelist that rejects
``caller_id``. This violates [[bsvibe-mcp-ui-parity]]. We expose the
NEW surface as a SEPARATE tool family so the legacy tools stay valid
for the legacy model-routing rules.

Handlers delegate to the same
:class:`SqlAlchemyRunRoutingRuleRepository` the REST surface uses, and
the input schema reuses
:func:`backend.api.v1.run_routing._validate_caller_id` so the MCP and
PWA paths land on one validation contract.

Scopes follow the existing convention: ``mcp:read`` for list,
``mcp:write`` for create / delete.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator
from sqlalchemy.exc import IntegrityError

from backend.api.v1.run_routing import (
    ApplyError,
    ApplyProposal,
    NoCompileModelError,
    SourceTextUninterpretableError,
    _conditions_from_compiled,
    _validate_caller_id,
    apply_proposals,
    compile_for_workspace,
    compile_source_text_for_workspace,
)
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.router.infrastructure.repositories import SqlAlchemyRunRoutingRuleRepository
from backend.router.routing.run_routing.db import RunRoutingRuleRow
from backend.router.routing.run_routing.engine import ALLOWED_FIELDS, VALID_OPERATORS


class _Envelope(RootModel[Any]):
    """Permissive output envelope — preserves the natural JSON shape."""


def _row_to_dict(row: RunRoutingRuleRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "caller_id": row.caller_id,
        "source_text": row.source_text,
        "priority": row.priority,
        "is_default": row.is_default,
        "target": row.target,
        "conditions": row.conditions if isinstance(row.conditions, list) else [],
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if isinstance(row.created_at, datetime) else None,
    }


# ---------------------------------------------------------------------------
# Schemas (mirror /api/v1/run-routing — RunRuleCreate)
# ---------------------------------------------------------------------------
class ConditionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    operator: str = "eq"
    value: Any = None
    negate: bool = False

    @field_validator("field")
    @classmethod
    def _field_allowed(cls, v: str) -> str:
        if v not in ALLOWED_FIELDS:
            allowed = ", ".join(sorted(ALLOWED_FIELDS))
            raise ValueError(f"unknown condition field {v!r}; allowed: {allowed}")
        return v

    @field_validator("operator")
    @classmethod
    def _operator_valid(cls, v: str) -> str:
        if v not in VALID_OPERATORS:
            raise ValueError(
                f"unknown operator {v!r}; allowed: {', '.join(sorted(VALID_OPERATORS))}"
            )
        return v


# ---------------------------------------------------------------------------
# bsvibe_run_routing_rules_list
# ---------------------------------------------------------------------------
class RunRoutingRulesListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _h_list(_args: RunRoutingRulesListInput, ctx: ToolContext) -> Any:
    repo = SqlAlchemyRunRoutingRuleRepository(ctx.session)
    rows = await repo.list_by_workspace(workspace_id=ctx.principal.workspace_id)
    return _Envelope([_row_to_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# bsvibe_run_routing_rules_create
# ---------------------------------------------------------------------------
class RunRoutingRulesCreateInput(BaseModel):
    """Mirror of :class:`backend.api.v1.run_routing.RunRuleCreate`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    # Lift N5 — the founder's free-text NL CONDITION phrase. When set, the rule
    # is compiled FROM it (caller_id/conditions derived, a category also creates
    # an intent def) and it is mutually exclusive with caller_id / conditions.
    source_text: str | None = Field(default=None, min_length=1, max_length=500)
    caller_id: str | None = Field(default=None, max_length=120)
    priority: int = Field(default=0, ge=0)
    is_default: bool = False
    target: str = Field(min_length=1, max_length=255)
    conditions: list[ConditionPayload] = Field(default_factory=list)
    is_active: bool = True

    @field_validator("caller_id")
    @classmethod
    def _caller_known(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_caller_id(v)

    @model_validator(mode="after")
    def _shape_valid(self) -> RunRoutingRulesCreateInput:
        # NL path: source_text owns the caller_id/conditions — reject a mix.
        if self.source_text is not None:
            if self.caller_id or self.conditions or self.is_default:
                raise ValueError(
                    "source_text is mutually exclusive with caller_id / conditions / is_default"
                )
            return self
        # Non-default STRUCTURED rules must declare a caller_id (top-level column
        # or a back-compat ``{field:'caller_id', operator:'eq'}`` condition).
        if self.is_default:
            return self
        if self.caller_id:
            return self
        condition_callers = [
            c
            for c in self.conditions
            if c.field == "caller_id" and c.operator == "eq" and isinstance(c.value, str)
        ]
        if not condition_callers:
            raise ValueError(
                "non-default run-routing rules must declare a caller_id "
                "(either the top-level field or a {field:'caller_id', operator:'eq'} condition)"
            )
        return self


async def _resolve_personal_account_id(ctx: ToolContext) -> uuid.UUID:
    """The personal billing account intents are scoped to (create-on-read) —
    the SAME account the N1 classifier reads at resolve time."""
    from backend.router.accounts.account_service import ensure_personal_account  # noqa: PLC0415

    account = await ensure_personal_account(ctx.session, workspace_id=ctx.principal.workspace_id)
    return account.id


async def _compile_source_text_or_error(
    ctx: ToolContext, source_text: str
) -> tuple[str | None, list[dict[str, Any]]]:
    """Compile an NL condition → (caller_id, conditions), creating an intent def
    for a category. Raises :class:`ToolError` on an uninterpretable phrase or a
    missing compile model."""
    account_id = await _resolve_personal_account_id(ctx)
    try:
        compiled = await compile_source_text_for_workspace(
            ctx.session, ctx.principal.workspace_id, source_text
        )
    except NoCompileModelError as exc:
        raise ToolError(
            "no model is configured to compile the condition with — set a default "
            "model or add a model account first"
        ) from exc
    except SourceTextUninterpretableError as exc:
        raise ToolError(
            f"could not interpret the condition {source_text!r} as a routing rule — "
            "try rephrasing (e.g. '복잡한 작업', '마케팅 관련', '한국어 요청')"
        ) from exc
    conditions = await _conditions_from_compiled(
        ctx.session,
        workspace_id=ctx.principal.workspace_id,
        account_id=account_id,
        compiled=compiled,
    )
    return compiled.caller_id, conditions


async def _h_create(args: RunRoutingRulesCreateInput, ctx: ToolContext) -> Any:
    repo = SqlAlchemyRunRoutingRuleRepository(ctx.session)
    caller_id = args.caller_id
    conditions = [c.model_dump() for c in args.conditions]
    if args.source_text is not None:
        caller_id, conditions = await _compile_source_text_or_error(ctx, args.source_text)
    row = RunRoutingRuleRow(
        id=uuid.uuid4(),
        workspace_id=ctx.principal.workspace_id,
        name=args.name,
        caller_id=caller_id,
        source_text=args.source_text,
        priority=args.priority,
        is_default=args.is_default,
        target=args.target,
        conditions=conditions,
        is_active=args.is_active,
    )
    try:
        await repo.add(row)
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ToolError(f"a run-routing rule named {args.name!r} already exists") from exc
    await ctx.session.commit()
    return _Envelope(_row_to_dict(row))


# ---------------------------------------------------------------------------
# bsvibe_run_routing_rules_delete
# ---------------------------------------------------------------------------
class RunRoutingRulesDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: uuid.UUID


class RunRoutingRulesDeleteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deleted: bool
    rule_id: str


async def _h_delete(args: RunRoutingRulesDeleteInput, ctx: ToolContext) -> Any:
    repo = SqlAlchemyRunRoutingRuleRepository(ctx.session)
    row = await repo.get(workspace_id=ctx.principal.workspace_id, rule_id=args.rule_id)
    if row is None:
        raise ToolError(f"run-routing rule not found: {args.rule_id}")
    await repo.delete(row)
    await ctx.session.commit()
    return RunRoutingRulesDeleteOutput(deleted=True, rule_id=str(args.rule_id))


# ---------------------------------------------------------------------------
# bsvibe_run_routing_rules_update (Lift 6 — edit an existing rule)
# ---------------------------------------------------------------------------
class RunRoutingRulesUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: uuid.UUID
    # Lift N5 — editing source_text recompiles + rewrites caller_id/conditions.
    source_text: str | None = Field(default=None, min_length=1, max_length=500)
    caller_id: str | None = Field(default=None, max_length=120)
    target: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None

    @field_validator("caller_id")
    @classmethod
    def _caller_known(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_caller_id(v)

    @model_validator(mode="after")
    def _source_text_not_mixed_with_caller(self) -> RunRoutingRulesUpdateInput:
        if self.source_text is not None and self.caller_id is not None:
            raise ValueError("source_text and caller_id are mutually exclusive on update")
        return self


async def _h_update(args: RunRoutingRulesUpdateInput, ctx: ToolContext) -> Any:
    repo = SqlAlchemyRunRoutingRuleRepository(ctx.session)
    row = await repo.get(workspace_id=ctx.principal.workspace_id, rule_id=args.rule_id)
    if row is None:
        raise ToolError(f"run-routing rule not found: {args.rule_id}")
    if args.source_text is not None:
        caller_id, conditions = await _compile_source_text_or_error(ctx, args.source_text)
        row.source_text = args.source_text
        row.caller_id = caller_id
        row.conditions = conditions
    if args.caller_id is not None:
        row.caller_id = args.caller_id
    if args.target is not None:
        row.target = args.target
    if args.is_active is not None:
        row.is_active = args.is_active
    await ctx.session.commit()
    return _Envelope(_row_to_dict(row))


# ---------------------------------------------------------------------------
# bsvibe_run_routing_rules_compile (Lift 5 — NL → proposals, dry-run)
# ---------------------------------------------------------------------------
class RunRoutingRulesCompileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)


async def _h_compile(args: RunRoutingRulesCompileInput, ctx: ToolContext) -> Any:
    try:
        proposals = await compile_for_workspace(ctx.session, ctx.principal.workspace_id, args.text)
    except NoCompileModelError as exc:
        raise ToolError(
            "no model is configured to compile with — set a default model or add "
            "a model account first"
        ) from exc
    return _Envelope({"proposals": proposals})


# ---------------------------------------------------------------------------
# bsvibe_run_routing_rules_compile_apply (Lift N3 — persist accepted proposals)
# ---------------------------------------------------------------------------
class RunRoutingRulesCompileApplyInput(BaseModel):
    """Mirror of :class:`backend.api.v1.run_routing.ApplyRequest`.

    Each proposal is one accepted item from ``bsvibe_run_routing_rules_compile``
    — exactly one dimension (caller / condition / category / default)."""

    model_config = ConfigDict(extra="forbid")

    proposals: list[ApplyProposal] = Field(min_length=1)


async def _h_compile_apply(args: RunRoutingRulesCompileApplyInput, ctx: ToolContext) -> Any:
    from backend.router.accounts.account_service import ensure_personal_account  # noqa: PLC0415

    account = await ensure_personal_account(ctx.session, workspace_id=ctx.principal.workspace_id)
    try:
        created = await apply_proposals(
            ctx.session,
            workspace_id=ctx.principal.workspace_id,
            account_id=account.id,
            proposals=args.proposals,
        )
    except ApplyError as exc:
        raise ToolError(str(exc)) from exc
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ToolError("a rule or intent with one of these names already exists") from exc
    return _Envelope(
        {
            "created": [_row_to_dict(r) for r in created],
            "default_set": any(p.is_default for p in args.proposals),
        }
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_run_routing_rules_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_run_routing_rules_list",
            description=(
                "List run-routing rules for the active workspace, priority "
                "ascending. These rules pick which ModelAccount handles a "
                "run (e.g. design → executor/codex, impl → executor/opencode). "
                "Distinct from bsvibe_routing_rules_* (those are model-routing "
                "rules for the litellm hook, a different layer)."
            ),
            input_schema=RunRoutingRulesListInput,
            output_schema=_Envelope,
            handler=_h_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_run_routing_rules_create",
            description=(
                "Create a run-routing rule. Mirrors POST /api/v1/run-routing. "
                "TWO ways to author: (1) NL-first — pass a free-text `source_text` "
                "CONDITION ('복잡한 작업', '마케팅 관련', '한국어 요청') + a `target` "
                "model; it compiles into the structured caller_id/conditions (a "
                "category also creates an intent def), and an uninterpretable "
                "phrase errors rather than persisting a dead rule. (2) Structured — "
                "name + caller_id + priority + target + optional conditions + "
                "optional is_default. source_text is mutually exclusive with "
                "caller_id/conditions/is_default. Non-default structured rules must "
                "declare a caller_id (validated against the caller registry — static "
                "known callers + skill.<name>). Conditions are validated against the "
                "engine ALLOWED_FIELDS."
            ),
            input_schema=RunRoutingRulesCreateInput,
            output_schema=_Envelope,
            handler=_h_create,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.run_routing_rules_create.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_run_routing_rules_delete",
            description="Delete a run-routing rule by id.",
            input_schema=RunRoutingRulesDeleteInput,
            output_schema=RunRoutingRulesDeleteOutput,
            handler=_h_delete,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.run_routing_rules_delete.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_run_routing_rules_update",
            description=(
                "Edit an existing run-routing rule. Mirrors PATCH "
                "/api/v1/run-routing/{id}: change source_text (recompiles + rewrites "
                "caller_id/conditions), caller_id, target, or is_active. source_text "
                "and caller_id are mutually exclusive; caller_id is validated against "
                "the registry."
            ),
            input_schema=RunRoutingRulesUpdateInput,
            output_schema=_Envelope,
            handler=_h_update,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.run_routing_rules_update.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_run_routing_rules_compile",
            description=(
                "Compile a plain-language routing description (e.g. '마케팅은 sonnet, "
                "복잡한 건 opus, 나머지는 haiku') into structured run-routing rule "
                "PROPOSALS. Dry-run — nothing is persisted. Detects which DIMENSION "
                "each clause is about (not just categories): a domain/category "
                "(classified_intent + intent_name + intent_examples), complexity "
                "(estimated_tokens / pipeline), language (detected_language), "
                "artifact (artifact_type_hint), execution stage (caller_id), or the "
                "catch-all default. Each proposal is validated against the caller "
                "registry + engine field/operator whitelist + the workspace's active "
                "model accounts. Apply the ones you want with "
                "bsvibe_run_routing_rules_compile_apply."
            ),
            input_schema=RunRoutingRulesCompileInput,
            output_schema=_Envelope,
            handler=_h_compile,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_run_routing_rules_compile_apply",
            description=(
                "Persist the accepted proposals from bsvibe_run_routing_rules_compile "
                "atomically. For a category proposal it creates the intent definition "
                "(name + seed examples) then the classified_intent rule; for a "
                "caller/condition proposal a plain rule; for the default proposal it "
                "sets the workspace default model account. All-or-nothing — any "
                "failure rolls back the whole batch."
            ),
            input_schema=RunRoutingRulesCompileApplyInput,
            output_schema=_Envelope,
            handler=_h_compile_apply,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.run_routing_rules_compile_apply.invoked",
        )
    )


__all__ = ["register_run_routing_rules_tools"]
