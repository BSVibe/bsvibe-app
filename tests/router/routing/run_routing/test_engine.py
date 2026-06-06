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
