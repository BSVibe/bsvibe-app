"""D4 — deterministic within-class selection among 2+ same-class accounts.

D2 (``tier_default``) picks the account CLASS (simple→local, substantial→
executor) but returns ``None`` when the desired class has 2+ active accounts,
degrading to the legacy single-active resolver — which raises an
``ambiguous_model_account`` founder Decision (a STALL). D4 closes that gap: the
class is D2's job, picking WITHIN the class is D4's.

These tests assert the DELTA D4 introduces — a real policy, not a stall:

1. No stall on 2+: two active same-class accounts resolve to a SPECIFIC one.
2. Policy is real: the winner CHANGES when the signal (routing_priority) changes
   (a fixed pick that ignores signals would fail this).
3. Fallthrough: the preferred account losing its edge falls through to the next.
4. D2 integration: tier picks the class, D4 picks within it — a substantial run
   with 2 active EXECUTOR accounts resolves to one; executor accounts stay
   excluded from the native pool.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from backend.execution.db import Decision, ExecutionRun, ExecutionRunActivity, RunStatus
from backend.router.accounts.models import ModelAccount
from backend.router.routing.run_routing.engine import resolve_route
from backend.router.routing.run_routing.multi_account import (
    ROUTING_PRIORITY_KEY,
    select_within_class,
)

from ...._support import memory_session


def _account(
    ws: uuid.UUID,
    litellm_model: str,
    *,
    provider: str = "executor",
    created_at: datetime | None = None,
    extra: dict | None = None,
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
        created_at=created_at or datetime.now(tz=UTC),
        updated_at=created_at or datetime.now(tz=UTC),
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


def _substantial_payload() -> dict:
    return {"frame": {"artifact_type_hint": "code", "pipeline": "design_then_impl"}}


def _simple_payload() -> dict:
    return {"frame": {"artifact_type_hint": "code", "pipeline": "single"}}


# ---------------------------------------------------------------------------
# Unit: the within-class policy itself (no DB) — deterministic, no stall.
# ---------------------------------------------------------------------------


def test_select_within_class_higher_priority_wins() -> None:
    ws = uuid.uuid4()
    low = _account(ws, "executor/opencode", extra={ROUTING_PRIORITY_KEY: 1})
    high = _account(ws, "executor/codex", extra={ROUTING_PRIORITY_KEY: 5})
    assert select_within_class([low, high]) is high
    # Input order must not matter — deterministic.
    assert select_within_class([high, low]) is high


def test_select_within_class_tiebreak_by_created_at_then_id() -> None:
    ws = uuid.uuid4()
    older = _account(ws, "executor/a", created_at=datetime(2024, 1, 1, tzinfo=UTC))
    newer = _account(ws, "executor/b", created_at=datetime(2024, 6, 1, tzinfo=UTC))
    # Equal (absent) priority → oldest created_at wins, stably.
    assert select_within_class([newer, older]) is older
    assert select_within_class([older, newer]) is older


def test_select_within_class_empty_and_single() -> None:
    ws = uuid.uuid4()
    assert select_within_class([]) is None
    only = _account(ws, "executor/solo")
    assert select_within_class([only]) is only


def test_priority_parsing_tolerates_string_bool_and_garbage() -> None:
    """``routing_priority`` is freeform JSON — coerce robustly, never raise.

    A numeric string parses; a bool / non-numeric string / absent value is the
    unconfigured priority 0 (then the stable tiebreak decides)."""
    ws = uuid.uuid4()
    base = datetime(2024, 1, 1, tzinfo=UTC)
    str_pri = _account(ws, "executor/str", created_at=base, extra={ROUTING_PRIORITY_KEY: "7"})
    bool_pri = _account(ws, "executor/bool", created_at=base, extra={ROUTING_PRIORITY_KEY: True})
    garbage = _account(ws, "executor/junk", created_at=base, extra={ROUTING_PRIORITY_KEY: "high"})
    # "7" parses to 7 → beats the bool/garbage (both coerce to 0).
    assert select_within_class([bool_pri, garbage, str_pri]) is str_pri


# ---------------------------------------------------------------------------
# Delta 1 — no stall on 2+: two active executor accounts resolve to a specific
# one through resolve_route (where D2 returned None → legacy raised a Decision).
# ---------------------------------------------------------------------------


async def test_two_same_class_accounts_no_stall() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        s.add_all(
            [
                _account(ws, "executor/opencode", provider="executor"),
                _account(ws, "executor/codex", provider="executor"),
            ]
        )
        run = _run(ws, _substantial_payload())
        s.add(run)
        await s.commit()

        resolved = await resolve_route(s, run)
        await s.commit()

        # A SPECIFIC account, not a stall.
        assert resolved is not None
        assert resolved.litellm_model in ("executor/opencode", "executor/codex")
        # And NO ambiguous-account founder Decision was raised.
        decisions = list(
            (await s.execute(select(Decision).where(Decision.run_id == run.id))).scalars().all()
        )
        assert decisions == []


# ---------------------------------------------------------------------------
# Delta 2 — the policy is REAL: inputs change the winner. Flip routing_priority
# and the resolved account flips with it (a fixed pick would fail this).
# ---------------------------------------------------------------------------


async def test_winner_changes_when_priority_changes() -> None:
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    async with memory_session() as s:
        # Workspace A: opencode has the higher priority.
        s.add_all(
            [
                _account(ws_a, "executor/opencode", extra={ROUTING_PRIORITY_KEY: 9}),
                _account(ws_a, "executor/codex", extra={ROUTING_PRIORITY_KEY: 1}),
            ]
        )
        # Workspace B: SAME two models, but codex now has the higher priority.
        s.add_all(
            [
                _account(ws_b, "executor/opencode", extra={ROUTING_PRIORITY_KEY: 1}),
                _account(ws_b, "executor/codex", extra={ROUTING_PRIORITY_KEY: 9}),
            ]
        )
        run_a = _run(ws_a, _substantial_payload())
        run_b = _run(ws_b, _substantial_payload())
        s.add_all([run_a, run_b])
        await s.commit()

        resolved_a = await resolve_route(s, run_a)
        resolved_b = await resolve_route(s, run_b)

        assert resolved_a is not None and resolved_b is not None
        # The signal — not a fixed pick — drove the choice: same models, flipped
        # priority, flipped winner.
        assert resolved_a.litellm_model == "executor/opencode"
        assert resolved_b.litellm_model == "executor/codex"


# ---------------------------------------------------------------------------
# Delta 3 — fallthrough: drop the preferred account's priority below a peer and
# the next-per-policy account wins (the policy reads the signal each time).
# ---------------------------------------------------------------------------


async def test_lower_priority_falls_through_to_next() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        # opencode is preferred (priority 5); codex is the fallback (priority 3).
        preferred = _account(ws, "executor/opencode", extra={ROUTING_PRIORITY_KEY: 5})
        fallback = _account(ws, "executor/codex", extra={ROUTING_PRIORITY_KEY: 3})
        s.add_all([preferred, fallback])
        run = _run(ws, _substantial_payload())
        s.add(run)
        await s.commit()

        # Preferred wins first.
        assert (await resolve_route(s, run)).litellm_model == "executor/opencode"

        # Founder demotes the preferred account below the fallback → next wins.
        preferred.extra_params = {ROUTING_PRIORITY_KEY: 1}
        await s.commit()
        run2 = _run(ws, _substantial_payload())
        s.add(run2)
        await s.commit()
        assert (await resolve_route(s, run2)).litellm_model == "executor/codex"


# ---------------------------------------------------------------------------
# Delta 4 — D2 integration: tier picks the class, D4 picks within it. A
# substantial run with 2 executors resolves to one; the local account is NEVER
# chosen (executors excluded from the native pool, locals from the executor
# class). And a glass-box routing_decision activity is recorded.
# ---------------------------------------------------------------------------


async def test_substantial_run_picks_within_executor_class_not_local() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        local = _account(ws, "ollama/qwen", provider="ollama", extra={ROUTING_PRIORITY_KEY: 99})
        s.add_all(
            [
                local,
                _account(
                    ws, "executor/opencode", provider="executor", extra={ROUTING_PRIORITY_KEY: 2}
                ),
                _account(
                    ws, "executor/codex", provider="executor", extra={ROUTING_PRIORITY_KEY: 8}
                ),
            ]
        )
        run = _run(ws, _substantial_payload())
        s.add(run)
        await s.commit()

        resolved = await resolve_route(s, run)
        await s.commit()

        # Within the EXECUTOR class — codex (priority 8) wins. The high-priority
        # LOCAL account is NOT eligible for a substantial run (wrong class).
        assert resolved is not None
        assert resolved.litellm_model == "executor/codex"
        assert resolved.provider == "executor"

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
        assert rows[0].payload["source"] == "tier_default_multi"
        assert rows[0].payload["tier"] == "substantial"
        assert rows[0].payload["target"] == "executor/codex"


async def test_simple_run_picks_within_local_class() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        s.add_all(
            [
                _account(ws, "ollama/qwen", provider="ollama", extra={ROUTING_PRIORITY_KEY: 1}),
                _account(ws, "ollama/llama", provider="ollama", extra={ROUTING_PRIORITY_KEY: 7}),
                _account(
                    ws, "executor/opencode", provider="executor", extra={ROUTING_PRIORITY_KEY: 99}
                ),
            ]
        )
        run = _run(ws, _simple_payload())
        s.add(run)
        await s.commit()

        resolved = await resolve_route(s, run)
        # Local class only; llama (priority 7) wins. Executor excluded.
        assert resolved is not None
        assert resolved.litellm_model == "ollama/llama"
        assert resolved.provider == "ollama"
