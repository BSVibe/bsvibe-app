"""Routing rule tools — UI-parity workflow surface (Lift D3b).

Wraps the model-routing rules the PWA's Settings → Models → ROUTING tab
(``RoutingRules.tsx``) drives via ``/api/v1/rules``. Each rule maps a
unit of work to a target LLM model (e.g. "Simple chores → local LLM").
Handlers are thin: validate input → call the EXISTING
:class:`RulesRepository` (the same repo the REST surface uses) so the
MCP and PWA paths land on one canonical mutation chain.

The MCP wire has no ``X-BSVibe-Account-Id`` header axis today, so the
caller's personal billing account is resolved via
:func:`ensure_personal_account` — mirroring the REST
:func:`backend.api.deps.require_account_id` get-or-create semantics
(:mod:`model_accounts_tools` does the same).

Scopes follow the existing convention: ``mcp:read`` for list,
``mcp:write`` for create / delete. The minimal create surface mirrors
the PWA's Add-rule form (name + target_model + priority + optional
default flag + optional simple conditions); complex multi-condition
editing is intentionally deferred (the REST update path doesn't expose
it either).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.router.accounts.account_service import ensure_personal_account
from backend.router.rules.conditions import ALLOWED_FIELDS
from backend.router.rules.db import RoutingRuleRow
from backend.router.rules.repository import RuleDuplicateError, RulesRepository


class _Envelope(RootModel[Any]):
    """Permissive output envelope — preserves the natural JSON shape."""


async def _resolve_account_id(ctx: ToolContext) -> uuid.UUID:
    """Resolve the workspace's personal billing account (REST parity)."""
    account = await ensure_personal_account(ctx.session, workspace_id=ctx.principal.workspace_id)
    return account.id


def _row_to_dict(row: RoutingRuleRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "priority": row.priority,
        "target_model": row.target_model,
        "is_default": row.is_default,
        "is_active": row.is_active,
        "conditions": [
            {
                "condition_type": c.condition_type,
                "field": c.field,
                "operator": c.operator,
                "value": c.value,
                "negate": c.negate,
            }
            for c in sorted(row.conditions, key=lambda c: (c.condition_type, c.field))
        ],
    }


# ---------------------------------------------------------------------------
# Schemas (mirror /api/v1/rules)
# ---------------------------------------------------------------------------
class ConditionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_type: str = Field(min_length=1, max_length=40)
    field: str = Field(min_length=1, max_length=60)
    operator: str = Field(default="eq", min_length=1, max_length=20)
    value: Any
    negate: bool = False

    @field_validator("field")
    @classmethod
    def _field_must_be_evaluable(cls, v: str) -> str:
        if v not in ALLOWED_FIELDS:
            raise ValueError(f"field {v!r} is not an evaluable condition field")
        return v


# ---------------------------------------------------------------------------
# bsvibe_routing_rules_list
# ---------------------------------------------------------------------------
class RoutingRulesListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _h_list(_args: RoutingRulesListInput, ctx: ToolContext) -> Any:
    account_id = await _resolve_account_id(ctx)
    repo = RulesRepository(ctx.session)
    rows = await repo.list_rules(workspace_id=ctx.principal.workspace_id, account_id=account_id)
    return _Envelope([_row_to_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# bsvibe_routing_rules_create
# ---------------------------------------------------------------------------
class RoutingRulesCreateInput(BaseModel):
    """Mirror of :class:`RuleCreate` (the REST schema).

    The PWA's Add-rule form posts the same shape; we accept it 1:1.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    target_model: str = Field(min_length=1, max_length=200)
    priority: int = Field(ge=1)
    is_default: bool = False
    is_active: bool = True
    conditions: list[ConditionPayload] = Field(default_factory=list)


async def _h_create(args: RoutingRulesCreateInput, ctx: ToolContext) -> Any:
    account_id = await _resolve_account_id(ctx)
    repo = RulesRepository(ctx.session)
    try:
        row = await repo.create_rule(
            workspace_id=ctx.principal.workspace_id,
            account_id=account_id,
            name=args.name,
            priority=args.priority,
            target_model=args.target_model,
            is_active=args.is_active,
            is_default=args.is_default,
        )
        if args.conditions:
            await repo.replace_conditions(row.id, [c.model_dump() for c in args.conditions])
    except RuleDuplicateError as exc:
        raise ToolError(f"a rule with that name or priority already exists: {args.name!r}") from exc
    await ctx.session.commit()
    fresh = await repo.get_rule(
        row.id, workspace_id=ctx.principal.workspace_id, account_id=account_id
    )
    if fresh is None:  # pragma: no cover — just created in this tx
        raise ToolError("routing rule disappeared after create")
    return _Envelope(_row_to_dict(fresh))


# ---------------------------------------------------------------------------
# bsvibe_routing_rules_delete
# ---------------------------------------------------------------------------
class RoutingRulesDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: uuid.UUID


class RoutingRulesDeleteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deleted: bool
    rule_id: str


async def _h_delete(args: RoutingRulesDeleteInput, ctx: ToolContext) -> Any:
    account_id = await _resolve_account_id(ctx)
    repo = RulesRepository(ctx.session)
    deleted = await repo.delete_rule(
        args.rule_id,
        workspace_id=ctx.principal.workspace_id,
        account_id=account_id,
    )
    if not deleted:
        raise ToolError(f"routing rule not found: {args.rule_id}")
    await ctx.session.commit()
    return RoutingRulesDeleteOutput(deleted=True, rule_id=str(args.rule_id))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_routing_rules_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_routing_rules_list",
            description=(
                "List model-routing rules for the active workspace + personal "
                "billing account, priority ascending. Empty list means every "
                "unit of work flows to the engine's default model account."
            ),
            input_schema=RoutingRulesListInput,
            output_schema=_Envelope,
            handler=_h_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_routing_rules_create",
            description=(
                "Create a model-routing rule. Mirrors the PWA's Add-rule form: "
                "name + target_model + priority + optional default flag + "
                "optional simple conditions. Conditions are validated against "
                "the engine's evaluable-field whitelist."
            ),
            input_schema=RoutingRulesCreateInput,
            output_schema=_Envelope,
            handler=_h_create,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.routing_rules_create.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_routing_rules_delete",
            description="Delete a model-routing rule by id.",
            input_schema=RoutingRulesDeleteInput,
            output_schema=RoutingRulesDeleteOutput,
            handler=_h_delete,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.routing_rules_delete.invoked",
        )
    )


__all__ = ["register_routing_rules_tools"]
