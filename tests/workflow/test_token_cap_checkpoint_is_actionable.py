"""#1105 — a run stopped at its token ceiling gets a question and a way forward.

Prod 2026-09-30, run ``92b76fba``: checkpoint ``fb38eb66`` had
``kind=run_token_cap_reached``, ``question=""``, ``options=null``,
``actions=null``. ``checkpoints_resolve(action_key=retry)`` answered "has no
one-click actions"; the only thing the founder could do was discard.

A plain retry would not have helped: usage ACCUMULATES on the run, so the
re-driven loop crosses the same ceiling on its first turn. So the retry here
GRANTS budget — one more full ceiling on top of what the run already used —
and both places that enforce the ceiling read that grant: the loop's
after-turn check (``token_budget``) and the in-session budget the adapter
sends the worker (#1104).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.workflow.application._checkpoint_shared import (
    ACTION_DISCARD,
    ACTION_RETRY,
    _decision_actions,
    _question_text,
)
from backend.workflow.application.checkpoint_resolution import resolve_checkpoint
from backend.workflow.application.token_budget import (
    TOKEN_CAP_DECISION_KIND,
    account_and_enforce_token_cap,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine

_CAP = 2_000_000
_USED = 2_100_000


def _cap_decision(run_id: uuid.UUID, workspace_id: uuid.UUID) -> Decision:
    """Shaped exactly like what ``account_and_enforce_token_cap`` records."""
    return Decision(
        id=uuid.uuid4(),
        run_id=run_id,
        workspace_id=workspace_id,
        decision=TOKEN_CAP_DECISION_KIND,
        payload={
            "reason": TOKEN_CAP_DECISION_KIND,
            "usage_total_tokens": _USED,
            "usage_prompt_tokens": _USED - 50_000,
            "usage_completion_tokens": 50_000,
            "token_cap": _CAP,
            "written_paths": [],
        },
        status=DecisionStatus.PENDING,
    )


class TestTheCheckpointIsNotBlank:
    @pytest.mark.parametrize("language", ["en", "ko"])
    def test_it_asks_a_question(self, language: str) -> None:
        decision = _cap_decision(uuid.uuid4(), uuid.uuid4())
        assert _question_text(decision, language).strip()

    def test_it_offers_more_budget_or_letting_go(self) -> None:
        decision = _cap_decision(uuid.uuid4(), uuid.uuid4())
        actions = _decision_actions(decision)
        assert actions is not None
        assert {a.key for a in actions} == {ACTION_RETRY, ACTION_DISCARD}


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_retry_grants_one_more_ceiling_and_resumes_the_run(sf) -> None:
    workspace_id = uuid.uuid4()
    async with sf() as s, workspace_session_scope(s, workspace_id):
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload={"intent_text": "fix the race"},
            usage_prompt_tokens=_USED - 50_000,
            usage_completion_tokens=50_000,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.flush()
        decision = _cap_decision(run.id, workspace_id)
        s.add(decision)
        await s.commit()

        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision.id,
            action_key=ACTION_RETRY,
            actor_id=uuid.uuid4(),
        )
        await s.commit()

        refreshed = await s.get(ExecutionRun, run.id)
        assert refreshed is not None
        assert refreshed.payload["token_cap_granted"] == _USED + _CAP
        assert refreshed.status is RunStatus.OPEN


def _orch(cap: int) -> SimpleNamespace:
    return SimpleNamespace(
        _settings=SimpleNamespace(agent_max_run_tokens=cap),
        _audit=AsyncMock(),
        _create_decision=AsyncMock(),
        _session=SimpleNamespace(commit=AsyncMock()),
        _decision_result=lambda *a, **k: "STOPPED",
    )


@pytest.mark.asyncio
async def test_the_loops_ceiling_honours_the_grant() -> None:
    """The re-driven run must not stop again at the base ceiling it already passed."""
    orch = _orch(_CAP)
    run = SimpleNamespace(
        usage_prompt_tokens=_USED - 50_000,
        usage_completion_tokens=50_000,
        payload={"token_cap_granted": _USED + _CAP},
    )
    turn = SimpleNamespace(usage_prompt_tokens=10_000, usage_completion_tokens=1_000)

    result = await account_and_enforce_token_cap(
        orch,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        work_step=None,  # type: ignore[arg-type]
        attempt=None,  # type: ignore[arg-type]
        turn=turn,  # type: ignore[arg-type]
        written_paths=[],
        final_text="",
    )

    assert result is None
    orch._create_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_grant_is_still_a_ceiling() -> None:
    """Control — past the GRANTED ceiling the run stops again."""
    orch = _orch(_CAP)
    run = SimpleNamespace(
        usage_prompt_tokens=_USED + _CAP,
        usage_completion_tokens=0,
        payload={"token_cap_granted": _USED + _CAP},
    )
    turn = SimpleNamespace(usage_prompt_tokens=1, usage_completion_tokens=0)

    result = await account_and_enforce_token_cap(
        orch,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        work_step=None,  # type: ignore[arg-type]
        attempt=None,  # type: ignore[arg-type]
        turn=turn,  # type: ignore[arg-type]
        written_paths=[],
        final_text="",
    )

    assert result == "STOPPED"
