"""Routing rule engine (Phase 1) — priority first-match + default fallback,
condition operators, and account resolution with safe legacy fallback."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from backend.accounts.models import ModelAccount
from backend.execution.db import ExecutionRun, RunStatus
from backend.routing.db import RunRoutingRuleRow
from backend.routing.engine import RoutingContext, evaluate_rules, resolve_route

from .._support import memory_session


def _rule(
    *,
    name: str,
    target: str,
    priority: int = 10,
    is_default: bool = False,
    conditions: list[dict] | None = None,
    is_active: bool = True,
    ws: uuid.UUID,
) -> RunRoutingRuleRow:
    return RunRoutingRuleRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        name=name,
        priority=priority,
        is_default=is_default,
        target=target,
        conditions=conditions or [],
        is_active=is_active,
    )


def _ctx(**kw) -> RoutingContext:
    return RoutingContext(**kw)


# ---------------------------------------------------------------------------
# evaluate_rules — pure matching logic
# ---------------------------------------------------------------------------


def test_first_match_by_priority_wins() -> None:
    ws = uuid.uuid4()
    rules = [
        _rule(
            name="code",
            target="executor/opencode",
            priority=20,
            ws=ws,
            conditions=[{"field": "artifact_type_hint", "operator": "eq", "value": "code"}],
        ),
        _rule(
            name="code-early",
            target="executor/codex",
            priority=10,
            ws=ws,
            conditions=[{"field": "artifact_type_hint", "operator": "eq", "value": "code"}],
        ),
    ]
    # priority 10 evaluated before 20 → codex wins.
    assert evaluate_rules(rules, _ctx(artifact_type_hint="code")) == "executor/codex"


def test_default_used_when_no_rule_matches() -> None:
    ws = uuid.uuid4()
    rules = [
        _rule(
            name="code",
            target="executor/opencode",
            ws=ws,
            conditions=[{"field": "artifact_type_hint", "operator": "eq", "value": "code"}],
        ),
        _rule(name="fallback", target="ollama/qwen", is_default=True, priority=999, ws=ws),
    ]
    assert evaluate_rules(rules, _ctx(artifact_type_hint="page")) == "ollama/qwen"


def test_no_match_no_default_returns_none() -> None:
    ws = uuid.uuid4()
    rules = [
        _rule(
            name="code",
            target="executor/opencode",
            ws=ws,
            conditions=[{"field": "artifact_type_hint", "operator": "eq", "value": "code"}],
        ),
    ]
    assert evaluate_rules(rules, _ctx(artifact_type_hint="page")) is None


def test_all_conditions_must_match_AND() -> None:
    ws = uuid.uuid4()
    rules = [
        _rule(
            name="design-code",
            target="executor/codex",
            ws=ws,
            conditions=[
                {"field": "artifact_type_hint", "operator": "eq", "value": "code"},
                {"field": "stage", "operator": "eq", "value": "design"},
            ],
        ),
    ]
    assert (
        evaluate_rules(rules, _ctx(artifact_type_hint="code", stage="design")) == "executor/codex"
    )
    # stage mismatch → no match.
    assert evaluate_rules(rules, _ctx(artifact_type_hint="code", stage="impl")) is None


def test_operators_contains_in_negate() -> None:
    ws = uuid.uuid4()
    contains = [
        _rule(
            name="c",
            target="t",
            ws=ws,
            conditions=[{"field": "intent_text", "operator": "contains", "value": "deploy"}],
        )
    ]
    assert evaluate_rules(contains, _ctx(intent_text="please DEPLOY the site")) == "t"

    in_rule = [
        _rule(
            name="i",
            target="t2",
            ws=ws,
            conditions=[{"field": "artifact_type_hint", "operator": "in", "value": ["code", "pr"]}],
        )
    ]
    assert evaluate_rules(in_rule, _ctx(artifact_type_hint="pr")) == "t2"

    negate = [
        _rule(
            name="n",
            target="t3",
            ws=ws,
            conditions=[
                {
                    "field": "path_classification",
                    "operator": "eq",
                    "value": "knowledge_only",
                    "negate": True,
                }
            ],
        )
    ]
    assert evaluate_rules(negate, _ctx(path_classification="agent_loop")) == "t3"
    assert evaluate_rules(negate, _ctx(path_classification="knowledge_only")) is None


def test_unknown_field_never_matches() -> None:
    ws = uuid.uuid4()
    rules = [
        _rule(
            name="bad",
            target="t",
            ws=ws,
            conditions=[{"field": "nonexistent", "operator": "eq", "value": "x"}],
        )
    ]
    assert evaluate_rules(rules, _ctx()) is None


def test_inactive_rule_skipped() -> None:
    ws = uuid.uuid4()
    rules = [
        _rule(
            name="off",
            target="t",
            is_active=False,
            ws=ws,
            conditions=[{"field": "artifact_type_hint", "operator": "eq", "value": "code"}],
        )
    ]
    assert evaluate_rules(rules, _ctx(artifact_type_hint="code")) is None


# ---------------------------------------------------------------------------
# resolve_route — DB-backed account resolution + legacy fallback
# ---------------------------------------------------------------------------


def _account(
    ws: uuid.UUID, litellm_model: str, provider: str = "executor", **extra
) -> ModelAccount:
    return ModelAccount(
        id=uuid.uuid4(),
        workspace_id=ws,
        account_id=uuid.uuid4(),
        provider=provider,
        label=litellm_model,
        litellm_model=litellm_model,
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="unknown",
        is_active=True,
        extra_params=extra or {},
    )


def _run(ws: uuid.UUID, payload: dict | None = None) -> ExecutionRun:
    return ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=ws,
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload=payload or {},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


async def test_resolve_no_rules_delegates_to_single_active() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        acct = _account(ws, "ollama/qwen", provider="ollama")
        s.add(acct)
        run = _run(ws)
        s.add(run)
        await s.commit()

        # No routing rules → legacy "exactly one active account" wins.
        resolved = await resolve_route(s, run)
        assert resolved is not None
        assert resolved.litellm_model == "ollama/qwen"


async def test_resolve_rule_selects_target_account_among_many_active() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        native = _account(ws, "ollama/qwen", provider="ollama")
        codex = _account(ws, "executor/codex")
        opencode = _account(ws, "executor/opencode")
        s.add_all([native, codex, opencode])
        # stage=design + code → codex.
        s.add(
            _rule(
                name="design",
                target="executor/codex",
                priority=10,
                ws=ws,
                conditions=[
                    {"field": "stage", "operator": "eq", "value": "design"},
                ],
            )
        )
        s.add(
            _rule(
                name="impl",
                target="executor/opencode",
                priority=20,
                ws=ws,
                conditions=[
                    {"field": "stage", "operator": "eq", "value": "impl"},
                ],
            )
        )
        s.add(_rule(name="fallback", target="ollama/qwen", is_default=True, priority=999, ws=ws))
        design_run = _run(ws, {"stage": "design", "frame": {"artifact_type_hint": "code"}})
        impl_run = _run(ws, {"stage": "impl", "frame": {"artifact_type_hint": "code"}})
        chat_run = _run(ws, {"frame": {"path_classification": "knowledge_only"}})
        s.add_all([design_run, impl_run, chat_run])
        await s.commit()

        assert (await resolve_route(s, design_run)).litellm_model == "executor/codex"
        assert (await resolve_route(s, impl_run)).litellm_model == "executor/opencode"
        # No stage → default rule → native.
        assert (await resolve_route(s, chat_run)).litellm_model == "ollama/qwen"


async def test_resolve_rule_target_inactive_falls_back_to_single_active() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        # Only the native account is active; the rule targets an executor that
        # isn't registered → safe fallback to the lone active account.
        s.add(_account(ws, "ollama/qwen", provider="ollama"))
        s.add(
            _rule(
                name="code",
                target="executor/opencode",
                ws=ws,
                conditions=[
                    {"field": "artifact_type_hint", "operator": "eq", "value": "code"},
                ],
            )
        )
        run = _run(ws, {"frame": {"artifact_type_hint": "code"}})
        s.add(run)
        await s.commit()

        resolved = await resolve_route(s, run)
        assert resolved is not None
        assert resolved.litellm_model == "ollama/qwen"
