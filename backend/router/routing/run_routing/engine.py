"""Routing rule evaluation + account resolution (Lift E2).

Priority-ordered, first-match rule engine: rules are sorted by ``priority``
ascending; the first active rule that matches the run's framed signals
(and, when ``caller_id`` is set, the calling site) wins; the default rule
catches anything unmatched.

Lift E2 deletes the tier-default fallback (``tier_class_accounts`` +
``select_tier_default_account`` + ``select_within_class``) and the
classifier path. When no rule matches AND the workspace has no
``default_account_id``, dispatch fails with
:class:`~backend.dispatch.resolver.NoMatchingRouteError` — never a silent
pick.

Conditions evaluate against a :class:`RoutingContext` built from the
run's framed signals. Only whitelisted fields are addressable (a typoed
field never matches and is logged) and the regex operator rejects
ReDoS-prone patterns — carried over from BSGateway's hardening.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.router.accounts.models import ModelAccount
    from backend.router.routing.run_routing.db import RunRoutingRuleRow
    from backend.workflow.infrastructure.db import ExecutionRun

logger = structlog.get_logger(__name__)

# Fields a condition may address — derived from the run's framed signals.
# Anything outside this set never matches (logged), so a typo can't silently
# persist as a no-op rule.
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "artifact_type_hint",  # "code" | "page" | "page_image" | "pr" | None
        "path_classification",  # "knowledge_only" | "agent_loop"
        "skill_match",  # matched skill name | None
        "intent_text",  # the run's intent / framed intent (text ops)
        "stage",  # "single" | "design" | "impl" (pipeline stage)
        "pipeline",  # "single" | "design_then_impl" (frame complexity verdict)
        "product_id",  # the run's product_id (str) | None
        # Lift E2 — caller_id condition clause (back-compat shape). New
        # rules persist caller_id on the column; legacy rows may carry it
        # as a condition clause instead.
        "caller_id",
    }
)

# Reject nested-quantifier regexes (ReDoS) — carried from BSGateway.
_REDOS_PATTERN = re.compile(r"\(.+[*+]\)[*+?]|\[.+[*+]\][*+?]")


@dataclass(frozen=True, slots=True)
class RoutingContext:
    """Pre-extracted run signals a rule evaluates against."""

    artifact_type_hint: str | None = None
    path_classification: str | None = None
    skill_match: str | None = None
    intent_text: str | None = None
    stage: str = "single"
    pipeline: str = "single"
    product_id: str | None = None
    # The dispatch caller_id for the LLM call this run is about to make.
    # ``None`` outside the run-routing path (callers route through the
    # resolver's column-first matcher instead).
    caller_id: str | None = None

    @classmethod
    def from_run(cls, run: ExecutionRun) -> RoutingContext:
        """Build the context from the run's payload (the frame the worker
        recorded) + run columns. Tolerant of a missing/odd payload."""
        payload: dict[str, Any] = run.payload if isinstance(run.payload, dict) else {}
        raw_frame = payload.get("frame")
        frame: dict[str, Any] = raw_frame if isinstance(raw_frame, dict) else {}
        intent = payload.get("intent_text") or frame.get("framed_intent") or payload.get("text")
        pipeline = frame.get("pipeline")
        return cls(
            artifact_type_hint=frame.get("artifact_type_hint"),
            path_classification=frame.get("path_classification"),
            skill_match=frame.get("skill_match"),
            intent_text=intent if isinstance(intent, str) else None,
            stage=_derive_stage(payload, frame),
            pipeline=pipeline if pipeline in ("single", "design_then_impl") else "single",
            product_id=str(run.product_id) if run.product_id is not None else None,
        )


def _derive_stage(payload: dict[str, Any], frame: dict[str, Any]) -> str:
    """Resolve the run's pipeline stage for routing."""
    explicit = payload.get("stage")
    if explicit:
        return str(explicit)
    if frame.get("pipeline") == "design_then_impl":
        return "design"
    return "single"


def _field_value(ctx: RoutingContext, field: str) -> Any:
    return getattr(ctx, field, None)


def _contains(haystack: Any, needle: Any) -> bool:
    if haystack is None:
        return False
    return str(needle).lower() in str(haystack).lower()


def _numeric(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _check_in(field_value: Any, expected: Any) -> bool:
    if not isinstance(expected, list):
        return False
    if isinstance(field_value, list):
        return bool(set(field_value) & set(expected))
    return field_value in expected


def _regex(field_value: Any, expected: Any) -> bool:
    pattern = str(expected)
    if len(pattern) > 500 or _REDOS_PATTERN.search(pattern):
        return False
    try:
        return bool(re.search(pattern, str(field_value), re.IGNORECASE))
    except re.error:
        return False


def _between(field_value: Any, expected: Any) -> bool:
    if not isinstance(expected, list) or len(expected) != 2:
        return False
    return _numeric(expected[0]) <= _numeric(field_value) <= _numeric(expected[1])


_OPERATORS: dict[str, Any] = {
    "eq": lambda fv, ex: bool(fv == ex),
    "contains": _contains,
    "regex": _regex,
    "gt": lambda fv, ex: _numeric(fv) > _numeric(ex),
    "lt": lambda fv, ex: _numeric(fv) < _numeric(ex),
    "gte": lambda fv, ex: _numeric(fv) >= _numeric(ex),
    "lte": lambda fv, ex: _numeric(fv) <= _numeric(ex),
    "between": _between,
    "in": _check_in,
    "not_in": lambda fv, ex: not _check_in(fv, ex),
}

#: The set of valid condition operators (the dispatch table's keys) — exported
#: so the rule-authoring API validates against the SAME source of truth.
VALID_OPERATORS: frozenset[str] = frozenset(_OPERATORS)


def _evaluate_raw(field_value: Any, operator: str, expected: Any) -> bool:
    fn = _OPERATORS.get(operator)
    return bool(fn(field_value, expected)) if fn is not None else False


def _evaluate_condition(condition: dict[str, Any], ctx: RoutingContext) -> bool:
    field = condition.get("field")
    if field not in ALLOWED_FIELDS:
        logger.warning("routing_condition_unknown_field", field=field)
        return False
    operator = condition.get("operator", "eq")
    if operator not in _OPERATORS:
        logger.warning("routing_condition_unknown_operator", operator=operator)
        return False
    result = _evaluate_raw(_field_value(ctx, field), operator, condition.get("value"))
    return (not result) if condition.get("negate") else result


_SKILL_CALLER_PREFIX = "skill."


def _caller_matches(column_caller: str, caller_id: str) -> bool:
    """The rule's ``caller_id`` column matches the dispatch caller. A skill
    caller (``skill.<name>``) also matches a rule authored with the bare
    ``<name>`` (back-compat tolerance, carried from the resolver's old
    matcher so #368's engine-delegation doesn't drop it)."""
    if column_caller == caller_id:
        return True
    return (
        caller_id.startswith(_SKILL_CALLER_PREFIX)
        and column_caller == caller_id[len(_SKILL_CALLER_PREFIX) :]
    )


def _rule_matches(rule: RunRoutingRuleRow, ctx: RoutingContext) -> bool:
    """A rule matches when its ``caller_id`` (when set) matches AND all
    conditions match. A rule with no caller_id and no conditions matches
    everything (catch-all default)."""
    column_caller = getattr(rule, "caller_id", None)
    if column_caller and ctx.caller_id and not _caller_matches(column_caller, ctx.caller_id):
        return False
    conditions = rule.conditions if isinstance(rule.conditions, list) else []
    return all(_evaluate_condition(c, ctx) for c in conditions if isinstance(c, dict))


def evaluate_rules(rules: list[RunRoutingRuleRow], ctx: RoutingContext) -> str | None:
    """Return the ``target`` of the first matching active rule by priority
    (ascending), else the active default rule's target, else ``None``."""
    default_target: str | None = None
    for rule in sorted(rules, key=lambda r: r.priority):
        if not rule.is_active:
            continue
        if rule.is_default:
            if default_target is None:
                default_target = rule.target
            continue
        if _rule_matches(rule, ctx):
            return rule.target
    return default_target


async def resolve_route(session: AsyncSession, run: ExecutionRun) -> ModelAccount | None:
    """Resolve the ModelAccount for ``run`` — Lift E2 simplified.

    Precedence:

    1. **Explicit founder rule** — an active
       :class:`~backend.router.routing.run_routing.db.RunRoutingRuleRow`
       (or the workspace's default rule) matches → return the active
       account whose ``litellm_model`` equals the rule's ``target``.
    2. **Workspace default ModelAccount** — :attr:`WorkspaceRow.default_account_id`.
    3. **No match** — return ``None`` so the caller can surface the
       founder-set fallback or raise. The classifier-driven tier_default
       + multi_account legacy paths are gone (Lift E2 removed the
       vocabulary entirely).

    The chosen target is recorded as an ``ExecutionRunActivity``
    (``activity_type="routing_decision"``) so routing stays glass-box.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from backend.identity.workspaces_db import WorkspaceRow  # noqa: PLC0415
    from backend.router.infrastructure.repositories import (  # noqa: PLC0415
        SqlAlchemyModelAccountRepository,
        SqlAlchemyRunRoutingRuleRepository,
    )

    ctx = RoutingContext.from_run(run)
    rules_repo = SqlAlchemyRunRoutingRuleRepository(session)
    rules = await rules_repo.list_by_workspace(workspace_id=run.workspace_id)
    accounts_repo = SqlAlchemyModelAccountRepository(session)

    # (1) Explicit founder rule wins when one matches an active account.
    if rules:
        target = evaluate_rules(rules, ctx)
        if target is not None:
            accounts = await accounts_repo.list_active_for_workspace(workspace_id=run.workspace_id)
            for account in accounts:
                if account.litellm_model == target:
                    logger.info(
                        "routing_rule_matched",
                        run_id=str(run.id),
                        workspace_id=str(run.workspace_id),
                        target=target,
                        stage=ctx.stage,
                    )
                    await _record_routing_decision(
                        session, run, source="explicit_rule", target=target
                    )
                    return account
            logger.warning(
                "routing_rule_target_not_active",
                run_id=str(run.id),
                target=target,
                hint="rule matched but no active account has this litellm_model",
            )

    # (2) Workspace default fallback — founder-set, never auto-stamped.
    default_id = await session.scalar(
        select(WorkspaceRow.default_account_id).where(WorkspaceRow.id == run.workspace_id)
    )
    if default_id is not None:
        accounts = await accounts_repo.list_active_for_workspace(workspace_id=run.workspace_id)
        for account in accounts:
            if account.id == default_id:
                logger.info(
                    "routing_workspace_default_applied",
                    run_id=str(run.id),
                    workspace_id=str(run.workspace_id),
                    target=account.litellm_model,
                )
                await _record_routing_decision(
                    session, run, source="workspace_default", target=account.litellm_model
                )
                return account

    # (3) No match — caller surfaces the error.
    logger.info(
        "routing_no_match",
        run_id=str(run.id),
        workspace_id=str(run.workspace_id),
    )
    return None


async def _record_routing_decision(
    session: AsyncSession,
    run: ExecutionRun,
    *,
    source: str,
    target: str,
) -> None:
    """Record the routing decision on the run's observability stream.

    Soft-fail: an activity hiccup must never break routing.
    """
    import uuid as _uuid  # noqa: PLC0415

    from backend.workflow.infrastructure.db import ExecutionRunActivity  # noqa: PLC0415

    try:
        session.add(
            ExecutionRunActivity(
                id=_uuid.uuid4(),
                run_id=run.id,
                workspace_id=run.workspace_id,
                activity_type="routing_decision",
                payload={"source": source, "target": target},
            )
        )
        await session.flush()
    except Exception:  # noqa: BLE001
        logger.warning("routing_decision_activity_failed", run_id=str(run.id), exc_info=True)


__all__ = [
    "ALLOWED_FIELDS",
    "VALID_OPERATORS",
    "RoutingContext",
    "evaluate_rules",
    "resolve_route",
]
