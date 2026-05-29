"""D2 — built-in tier default routing.

§12 model-tiering lock: simple chores → the local LLM account; substantial work
+ orchestration → the cloud/opencode (executor) baseline. The frame's
``pipeline`` complexity verdict (D1 #212 — the LLM judges by complexity, not
keywords) is the run-level tier signal: ``single`` → simple → local;
``design_then_impl`` → substantial → executor.

These tests assert the DELTA D2 introduces: with ZERO founder routing rules, a
simple framed run and a substantial framed run now resolve to DIFFERENT accounts
(local vs executor) — where today both fall through to the single-active
resolver. Precedence (explicit rule > tier default > single-active),
backward-compat (chat / no-pipeline runs still resolve), and the glass-box
audit trail are also asserted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from backend.accounts.models import ModelAccount
from backend.execution.db import ExecutionRun, ExecutionRunActivity, RunStatus
from backend.routing.db import RunRoutingRuleRow
from backend.routing.engine import RoutingContext, resolve_route

from .._support import memory_session


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


def _simple_payload() -> dict:
    # A focused one-pass code tweak — the frame judged it ``single``.
    return {"frame": {"artifact_type_hint": "code", "pipeline": "single"}}


def _substantial_payload() -> dict:
    # A multi-component build — the frame judged it ``design_then_impl``.
    return {"frame": {"artifact_type_hint": "code", "pipeline": "design_then_impl"}}


# ---------------------------------------------------------------------------
# RoutingContext now carries the run-level tier signal (the frame's pipeline).
# ---------------------------------------------------------------------------


def test_context_exposes_pipeline_from_frame() -> None:
    ws = uuid.uuid4()
    simple = RoutingContext.from_run(_run(ws, _simple_payload()))
    substantial = RoutingContext.from_run(_run(ws, _substantial_payload()))
    assert simple.pipeline == "single"
    assert substantial.pipeline == "design_then_impl"


# ---------------------------------------------------------------------------
# Delta 1 — zero founder rules: simple → local, substantial → executor.
# (Today both fall to single-active; assert they now DIVERGE.)
# ---------------------------------------------------------------------------


async def test_no_rules_simple_run_routes_to_local_account() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        local = _account(ws, "ollama/qwen", provider="ollama")
        executor = _account(ws, "executor/opencode", provider="executor")
        s.add_all([local, executor])
        run = _run(ws, _simple_payload())
        s.add(run)
        await s.commit()

        resolved = await resolve_route(s, run)
        assert resolved is not None
        assert resolved.litellm_model == "ollama/qwen"


async def test_no_rules_substantial_run_routes_to_executor_account() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        local = _account(ws, "ollama/qwen", provider="ollama")
        executor = _account(ws, "executor/opencode", provider="executor")
        s.add_all([local, executor])
        run = _run(ws, _substantial_payload())
        s.add(run)
        await s.commit()

        resolved = await resolve_route(s, run)
        assert resolved is not None
        assert resolved.litellm_model == "executor/opencode"


async def test_no_rules_simple_and_substantial_diverge() -> None:
    """The core D2 delta: with no founder rules the two runs no longer resolve
    to the same account — the tier verdict now steers selection."""
    ws = uuid.uuid4()
    async with memory_session() as s:
        s.add_all(
            [
                _account(ws, "ollama/qwen", provider="ollama"),
                _account(ws, "executor/opencode", provider="executor"),
            ]
        )
        simple = _run(ws, _simple_payload())
        substantial = _run(ws, _substantial_payload())
        s.add_all([simple, substantial])
        await s.commit()

        simple_acct = await resolve_route(s, simple)
        substantial_acct = await resolve_route(s, substantial)
        assert simple_acct is not None and substantial_acct is not None
        assert simple_acct.litellm_model != substantial_acct.litellm_model
        assert simple_acct.litellm_model == "ollama/qwen"
        assert substantial_acct.litellm_model == "executor/opencode"


# ---------------------------------------------------------------------------
# Delta 2 — precedence: an explicit matching rule still beats the tier default.
# ---------------------------------------------------------------------------


async def test_explicit_rule_wins_over_tier_default() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        s.add_all(
            [
                _account(ws, "ollama/qwen", provider="ollama"),
                _account(ws, "executor/opencode", provider="executor"),
                _account(ws, "executor/codex", provider="executor"),
            ]
        )
        # Founder pins ALL code work to codex — overrides the tier default that
        # would send a substantial code run to opencode.
        s.add(
            RunRoutingRuleRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                name="all-code-codex",
                priority=10,
                is_default=False,
                target="executor/codex",
                conditions=[{"field": "artifact_type_hint", "operator": "eq", "value": "code"}],
                is_active=True,
            )
        )
        substantial = _run(ws, _substantial_payload())
        s.add(substantial)
        await s.commit()

        resolved = await resolve_route(s, substantial)
        assert resolved is not None
        assert resolved.litellm_model == "executor/codex"


# ---------------------------------------------------------------------------
# Delta 3 — backward-compat: a chat / non-build run with no rules still
# resolves (no stall, no raise). pipeline absent → single → local, and a
# pure single-active workspace is unchanged.
# ---------------------------------------------------------------------------


async def test_chat_run_no_pipeline_no_rules_still_resolves() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        # A non-build chat run: no pipeline in the frame at all.
        s.add(_account(ws, "ollama/qwen", provider="ollama"))
        run = _run(ws, {"frame": {"path_classification": "agent_loop"}})
        s.add(run)
        await s.commit()

        resolved = await resolve_route(s, run)
        assert resolved is not None
        assert resolved.litellm_model == "ollama/qwen"


async def test_single_active_workspace_unchanged_for_substantial_run() -> None:
    """A workspace with only ONE active account (no executor registered) keeps
    resolving to it even for a substantial run — the tier default degrades
    loudly: no executor class present → safe single-active fallback, never a
    silent wrong pick."""
    ws = uuid.uuid4()
    async with memory_session() as s:
        s.add(_account(ws, "ollama/qwen", provider="ollama"))
        run = _run(ws, _substantial_payload())
        s.add(run)
        await s.commit()

        resolved = await resolve_route(s, run)
        assert resolved is not None
        assert resolved.litellm_model == "ollama/qwen"


# ---------------------------------------------------------------------------
# Delta 4 — glass-box: the resolved tier + target appear in the run's audit
# events (ExecutionRunActivity rows — the run's observability stream).
# ---------------------------------------------------------------------------


async def test_tier_default_records_glassbox_activity() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        s.add_all(
            [
                _account(ws, "ollama/qwen", provider="ollama"),
                _account(ws, "executor/opencode", provider="executor"),
            ]
        )
        run = _run(ws, _substantial_payload())
        s.add(run)
        await s.commit()

        await resolve_route(s, run)
        await s.commit()

        rows = list(
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.run_id == run.id,
                        ExecutionRunActivity.activity_type == "routing_decision",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        payload = rows[0].payload
        assert payload["source"] == "tier_default"
        assert payload["tier"] == "substantial"
        assert payload["target"] == "executor/opencode"
