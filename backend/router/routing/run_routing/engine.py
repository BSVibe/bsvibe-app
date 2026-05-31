"""Routing rule evaluation + account resolution (Phase 1).

Priority-ordered, first-match rule engine (BSGateway-faithful): rules are
sorted by ``priority`` ascending; the first non-default rule whose conditions
ALL match wins; otherwise the ``is_default`` rule (if any) wins. Each rule's
``target`` is matched against the workspace's active ModelAccounts by
``litellm_model`` to pick the run's compute (native LLM or executor CLI).

Conditions evaluate against a :class:`RoutingContext` built from the run's
framed signals. Only whitelisted fields are addressable (a typoed field never
matches and is logged) and the regex operator rejects ReDoS-prone patterns —
both carried over from BSGateway's hardening.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.execution.db import ExecutionRun
    from backend.router.accounts.models import ModelAccount
    from backend.router.routing.run_routing.db import RunRoutingRuleRow

logger = structlog.get_logger(__name__)

# Fields a condition may address — derived from the run's framed signals.
# Anything outside this set never matches (logged), so a typo can't silently
# persist as a no-op rule (BSGateway audit H4).
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "artifact_type_hint",  # "code" | "page" | "page_image" | "pr" | None
        "path_classification",  # "knowledge_only" | "agent_loop"
        "skill_match",  # matched skill name | None
        "intent_text",  # the run's intent / framed intent (text ops)
        "stage",  # "single" | "design" | "impl" (pipeline stage)
        "pipeline",  # "single" | "design_then_impl" (frame complexity verdict)
        "product_id",  # the run's product_id (str) | None
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
    """Resolve the run's pipeline stage for routing.

    Only the spawned implementation run carries an explicit ``stage="impl"``;
    the FIRST run of a ``design_then_impl`` pipeline never has its stage set
    (the orchestrator chains impl off the frame's ``pipeline`` signal, not a
    stage column). So when there is no explicit stage but the frame marks the
    pipeline ``design_then_impl``, this is the design stage — derive it so the
    ``stage==design`` rule routes the first run to the designer. Everything
    else is a plain ``single`` run.
    """
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


# Operator dispatch — flat table keeps the branch/return count low and makes
# the supported set self-documenting.
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


def _rule_matches(rule: RunRoutingRuleRow, ctx: RoutingContext) -> bool:
    """A rule matches when ALL its conditions match (AND). A rule with no
    conditions matches everything (useful for a catch-all default)."""
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
    """Resolve the ModelAccount for ``run`` — precedence, highest → lowest:

    1. **Explicit founder rule** — an active :class:`RunRoutingRuleRow` (or the
       workspace's default rule) matches → return the active account whose
       ``litellm_model`` equals the rule's ``target``.
    2. **Built-in tier default** (D2 / §12) — no explicit rule matched: apply the
       frame's complexity verdict. ``pipeline == "single"`` → the active LOCAL
       account; ``pipeline == "design_then_impl"`` → the active EXECUTOR
       (cloud/opencode) account. D2 picks the CLASS; when EXACTLY ONE account of
       that class is active it returns it directly.
    2b. **D4 within-class policy** — the desired class has 2+ active accounts
       (D2 returned ``None`` here, gotcha #200). Rather than stall on the legacy
       resolver's ``ambiguous_model_account`` Decision, pick deterministically
       within the class (:func:`~backend.router.routing.run_routing.multi_account.select_within_class`
       — highest ``routing_priority``, tiebroken by ``created_at`` then ``id``).
       The class is D2's job, picking within it is D4's.
    3. **Legacy single-active fallback** — none above resolved (no class match at
       all, or zero accounts): the lone active account, or a founder
       :class:`Decision` on zero. Never crashes, never silently guesses.

    The chosen tier + resolved target are recorded as an ``ExecutionRunActivity``
    (``activity_type="routing_decision"``) so routing is glass-box.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from backend.router.routing.run_routing.db import RunRoutingRuleRow  # noqa: PLC0415
    from backend.router.routing.run_routing.multi_account import (  # noqa: PLC0415
        select_within_class,
    )
    from backend.router.routing.run_routing.tier_default import (  # noqa: PLC0415
        select_tier_default_account,
        tier_class_accounts,
        tier_from_context,
    )
    from backend.workflow.infrastructure.workers.run import (  # noqa: PLC0415 — avoid import cycle
        _list_active_workspace_accounts,
        resolve_workspace_model_account,
    )

    ctx = RoutingContext.from_run(run)
    rules = list(
        (
            await session.execute(
                select(RunRoutingRuleRow).where(RunRoutingRuleRow.workspace_id == run.workspace_id)
            )
        )
        .scalars()
        .all()
    )

    # (1) Explicit founder rules win when one matches an active account.
    if rules:
        target = evaluate_rules(rules, ctx)
        if target is not None:
            accounts = await _list_active_workspace_accounts(session, run.workspace_id)
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
                        session, run, source="explicit_rule", tier=None, target=target
                    )
                    return account
            logger.warning(
                "routing_rule_target_not_active",
                run_id=str(run.id),
                target=target,
                hint="rule matched but no active account has this litellm_model",
            )

    # (2) Built-in tier default — §12 auto-routing when no explicit rule matched.
    accounts = await _list_active_workspace_accounts(session, run.workspace_id)
    tier = tier_from_context(ctx)
    tier_account = select_tier_default_account(tier, accounts)
    if tier_account is not None:
        logger.info(
            "routing_tier_default_applied",
            run_id=str(run.id),
            workspace_id=str(run.workspace_id),
            tier=tier,
            target=tier_account.litellm_model,
            pipeline=ctx.pipeline,
        )
        await _record_routing_decision(
            session,
            run,
            source="tier_default",
            tier=tier,
            target=tier_account.litellm_model,
        )
        return tier_account

    # (2b) D4 — the desired class has 2+ active accounts: pick within it
    # deterministically instead of stalling on the legacy ambiguous Decision.
    class_candidates = tier_class_accounts(tier, accounts)
    multi_account = select_within_class(class_candidates)
    if multi_account is not None:
        logger.info(
            "routing_multi_account_applied",
            run_id=str(run.id),
            workspace_id=str(run.workspace_id),
            tier=tier,
            target=multi_account.litellm_model,
            pipeline=ctx.pipeline,
            candidate_count=len(class_candidates),
        )
        await _record_routing_decision(
            session,
            run,
            source="tier_default_multi",
            tier=tier,
            target=multi_account.litellm_model,
        )
        return multi_account

    # (3) Safe legacy fallback (lone active account, else founder Decision).
    return await resolve_workspace_model_account(session, run)


async def _record_routing_decision(
    session: AsyncSession,
    run: ExecutionRun,
    *,
    source: str,
    tier: str | None,
    target: str,
) -> None:
    """Record the routing decision on the run's observability stream so the
    resolved tier + target are glass-box. Soft-fail: an activity hiccup must
    never break routing (resolution already succeeded)."""
    import uuid as _uuid  # noqa: PLC0415

    from backend.execution.db import ExecutionRunActivity  # noqa: PLC0415

    try:
        session.add(
            ExecutionRunActivity(
                id=_uuid.uuid4(),
                run_id=run.id,
                workspace_id=run.workspace_id,
                activity_type="routing_decision",
                payload={"source": source, "tier": tier, "target": target},
            )
        )
        await session.flush()
    except Exception:  # noqa: BLE001 — observability must never break routing
        logger.warning("routing_decision_activity_failed", run_id=str(run.id), exc_info=True)


__all__ = [
    "ALLOWED_FIELDS",
    "VALID_OPERATORS",
    "RoutingContext",
    "evaluate_rules",
    "resolve_route",
]
