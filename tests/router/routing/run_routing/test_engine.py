"""Routing rule engine — priority first-match + default fallback,
condition operators, and account resolution (Lift E2 — no tier fallback).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from backend.identity.workspaces_db import WorkspaceRow
from backend.router.accounts.models import ModelAccount
from backend.router.routing.run_routing.db import RunRoutingRuleRow
from backend.router.routing.run_routing.engine import (
    RoutingContext,
    evaluate_rules,
    resolve_route,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from ...._support import memory_session


def _rule(
    *,
    name: str,
    target: str,
    priority: int = 10,
    is_default: bool = False,
    conditions: list[dict] | None = None,
    is_active: bool = True,
    caller_id: str | None = None,
    ws: uuid.UUID,
) -> RunRoutingRuleRow:
    return RunRoutingRuleRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        name=name,
        caller_id=caller_id,
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
    assert evaluate_rules(rules, _ctx(artifact_type_hint="pr")) == "ollama/qwen"


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
    assert evaluate_rules(rules, _ctx(artifact_type_hint="pr")) is None


def test_unknown_field_logged_and_skipped() -> None:
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


def test_caller_id_column_filters_match() -> None:
    ws = uuid.uuid4()
    rule = _rule(
        name="frame",
        target="ollama/qwen",
        ws=ws,
        caller_id="workflow.frame",
        conditions=[],
    )
    # caller_id matches → match (no other conditions to fail).
    assert (
        evaluate_rules([rule], _ctx(caller_id="workflow.frame", artifact_type_hint=None))
        == "ollama/qwen"
    )
    # Different caller → no match.
    assert evaluate_rules([rule], _ctx(caller_id="workflow.judge", artifact_type_hint=None)) is None


# ---------------------------------------------------------------------------
# RoutingContext.from_run
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


def test_from_run_derives_design_stage_from_pipeline() -> None:
    ws = uuid.uuid4()
    design = RoutingContext.from_run(
        _run(ws, {"frame": {"artifact_type_hint": "code", "pipeline": "design_then_impl"}})
    )
    assert design.stage == "design"
    impl = RoutingContext.from_run(
        _run(ws, {"stage": "impl", "frame": {"pipeline": "design_then_impl"}})
    )
    assert impl.stage == "impl"
    single = RoutingContext.from_run(_run(ws, {"frame": {"pipeline": "single"}}))
    assert single.stage == "single"


# ---------------------------------------------------------------------------
# Lift 1 (unified routing) — content signals absorbed from legacy Layer 2
# (estimated_tokens / classified_intent / detected_language). Additive: the
# new fields are addressable and populated when derivable, but adding them
# must not change how any pre-existing rule matches.
# ---------------------------------------------------------------------------


def test_content_signal_fields_are_addressable() -> None:
    from backend.router.routing.run_routing.engine import ALLOWED_FIELDS

    assert {"estimated_tokens", "classified_intent", "detected_language"} <= ALLOWED_FIELDS


def test_content_signals_default_to_empty_when_absent() -> None:
    ctx = _ctx()
    assert ctx.estimated_tokens == 0
    assert ctx.classified_intent is None
    assert ctx.detected_language is None


def test_from_run_estimates_tokens_from_intent_text() -> None:
    ws = uuid.uuid4()
    ctx = RoutingContext.from_run(_run(ws, {"intent_text": "build a login page with oauth"}))
    assert ctx.estimated_tokens > 0
    # No text → zero, never a crash.
    assert RoutingContext.from_run(_run(ws, {})).estimated_tokens == 0


def test_from_run_detects_language_from_intent_text() -> None:
    ws = uuid.uuid4()
    ko = RoutingContext.from_run(_run(ws, {"intent_text": "로그인 페이지를 만들어줘"}))
    assert ko.detected_language == "ko"
    en = RoutingContext.from_run(_run(ws, {"intent_text": "build a login page"}))
    assert en.detected_language == "en"
    # No text → None.
    assert RoutingContext.from_run(_run(ws, {})).detected_language is None


def test_from_run_reads_classified_intent_from_frame() -> None:
    ws = uuid.uuid4()
    ctx = RoutingContext.from_run(_run(ws, {"frame": {"classified_intent": "code_generation"}}))
    assert ctx.classified_intent == "code_generation"
    # Missing / non-str → None.
    assert RoutingContext.from_run(_run(ws, {"frame": {}})).classified_intent is None


def test_rule_can_match_on_content_signal() -> None:
    ws = uuid.uuid4()
    heavy = _rule(
        name="big-context",
        target="executor/opus",
        ws=ws,
        conditions=[{"field": "estimated_tokens", "operator": "gt", "value": 1000}],
    )
    assert evaluate_rules([heavy], _ctx(estimated_tokens=5000)) == "executor/opus"
    assert evaluate_rules([heavy], _ctx(estimated_tokens=10)) is None

    korean = _rule(
        name="ko-route",
        target="executor/sonnet",
        ws=ws,
        conditions=[{"field": "detected_language", "operator": "eq", "value": "ko"}],
    )
    assert evaluate_rules([korean], _ctx(detected_language="ko")) == "executor/sonnet"
    assert evaluate_rules([korean], _ctx(detected_language="en")) is None


def test_content_signals_do_not_change_existing_rule_matching() -> None:
    """Additive guarantee: a rule authored against the old field set matches
    exactly as before, whatever the new signals hold."""
    ws = uuid.uuid4()
    rule = _rule(
        name="code",
        target="executor/codex",
        ws=ws,
        conditions=[{"field": "artifact_type_hint", "operator": "eq", "value": "code"}],
    )
    with_signals = _ctx(
        artifact_type_hint="code",
        estimated_tokens=9999,
        classified_intent="anything",
        detected_language="ja",
    )
    assert evaluate_rules([rule], with_signals) == "executor/codex"
    assert evaluate_rules([rule], _ctx(artifact_type_hint="pr", estimated_tokens=9999)) is None


# ---------------------------------------------------------------------------
# resolve_route — DB-backed, no tier fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_no_rules_no_default_returns_none() -> None:
    ws_id = uuid.uuid4()
    async with memory_session() as s:
        s.add(WorkspaceRow(id=ws_id, name="w", region="us-1", safe_mode=True, legal_basis="x"))
        acct = _account(ws_id, "ollama/qwen", provider="ollama")
        s.add(acct)
        run = _run(ws_id)
        s.add(run)
        await s.commit()
        # No rules + no default = no match (Lift E2 dropped the
        # "exactly one active" tier fallback).
        assert await resolve_route(s, run) is None


@pytest.mark.asyncio
async def test_resolve_workspace_default_used_when_no_rules() -> None:
    ws_id = uuid.uuid4()
    async with memory_session() as s:
        acct = _account(ws_id, "ollama/qwen", provider="ollama")
        s.add(acct)
        s.add(
            WorkspaceRow(
                id=ws_id,
                name="w",
                region="us-1",
                safe_mode=True,
                legal_basis="x",
                default_account_id=acct.id,
            )
        )
        run = _run(ws_id)
        s.add(run)
        await s.commit()
        resolved = await resolve_route(s, run)
        assert resolved is not None
        assert resolved.litellm_model == "ollama/qwen"


@pytest.mark.asyncio
async def test_resolve_rule_match_beats_workspace_default() -> None:
    ws_id = uuid.uuid4()
    async with memory_session() as s:
        native = _account(ws_id, "ollama/qwen", provider="ollama")
        codex = _account(ws_id, "executor/codex")
        s.add_all([native, codex])
        s.add(
            WorkspaceRow(
                id=ws_id,
                name="w",
                region="us-1",
                safe_mode=True,
                legal_basis="x",
                default_account_id=native.id,
            )
        )
        s.add(
            _rule(
                name="design",
                target="executor/codex",
                priority=10,
                ws=ws_id,
                conditions=[{"field": "stage", "operator": "eq", "value": "design"}],
            )
        )
        run = _run(ws_id, {"stage": "design", "frame": {}})
        s.add(run)
        await s.commit()
        resolved = await resolve_route(s, run)
        assert resolved is not None
        assert resolved.litellm_model == "executor/codex"


@pytest.mark.asyncio
async def test_resolve_rule_target_inactive_falls_through() -> None:
    ws_id = uuid.uuid4()
    async with memory_session() as s:
        s.add(WorkspaceRow(id=ws_id, name="w", region="us-1", safe_mode=True, legal_basis="x"))
        s.add(_account(ws_id, "ollama/qwen", provider="ollama"))
        s.add(
            _rule(
                name="code",
                target="executor/opencode",  # not active in workspace
                ws=ws_id,
                conditions=[
                    {"field": "artifact_type_hint", "operator": "eq", "value": "code"},
                ],
            )
        )
        run = _run(ws_id, {"frame": {"artifact_type_hint": "code"}})
        s.add(run)
        await s.commit()
        # Rule's target is inactive + no workspace default = None.
        assert await resolve_route(s, run) is None
