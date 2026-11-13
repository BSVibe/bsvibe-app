"""질문에 창업자가 직접 쓴 답도 장부 정정이다 — 옵션 선택·액션 키는 아니다."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.workflow.application.checkpoint_resolution import resolve_checkpoint
from backend.workflow.application.requirements_ledger import judge_ledger
from backend.workflow.domain.requirements import (
    FOUNDER_CORRECTIONS_KEY,
    GateFacts,
    founder_corrections,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)
from tests._support import db_engine

pytestmark = pytest.mark.asyncio

C1 = "항상 값으로 취급하라"
ANSWER = "X(항상 값)가 아니라 Y: `-`로 시작하면 플래그다"
AS_Y = "if nxt.startswith('-'): treat_as_flag()"
AS_X = "value = argv[i + 1]"
REQUIREMENTS = [{"id": "R2", "text": "다음 단어는 항상 값이다", "kind": "do", "status": "pending"}]
EVIDENCE = [{"requirement_id": "R2", "paths": ["cov.py"], "tests": [], "note": "n"}]
FACTS = GateFacts(diff_paths=frozenset({"cov.py"}), passed_commands=())


@pytest.fixture
async def sf():
    async with db_engine() as (engine, _pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _resolve(
    sf,
    *,
    options: list[str] | None,
    answer: str = "",
    action_key: str | None = None,
    kind: str = "ask_user_question",
) -> dict[str, Any]:
    ws, actor = uuid.uuid4(), uuid.uuid4()
    async with sf() as s, workspace_session_scope(s, ws):
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=ws,
            status=RunStatus.RUNNING,
            payload={FOUNDER_CORRECTIONS_KEY: [{"decision_id": "c1", "text": C1}]},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.flush()
        q: dict[str, Any] = {"question": "어느 쪽?"}
        if options:
            q["options"] = options
        d = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=ws,
            decision=kind,
            payload=q,
            status=DecisionStatus.PENDING,
        )
        s.add(d)
        await s.flush()
        await resolve_checkpoint(
            s,
            workspace_id=ws,
            checkpoint_id=d.id,
            answer=answer,
            action_key=action_key,
            actor_id=actor,
        )
        await s.commit()
        await s.refresh(run)
        return dict(run.payload)


async def _judge(code: str, corrections: list[str]) -> str:
    async def excerpt(_row: Any) -> str:
        return f"--- cov.py\n{code}"

    async def complete(messages: list[dict[str, str]]) -> str:
        user = messages[1]["content"]
        if "FOUNDER CORRECTIONS" in user:
            ok = AS_Y in user
            return json.dumps(
                {"passed": ok, "reasoning": "r", **({"followed_correction": True} if ok else {})}
            )
        return json.dumps({"passed": AS_X in user, "reasoning": "r"})

    rows = await judge_ledger(
        REQUIREMENTS, EVIDENCE, FACTS, excerpt=excerpt, complete=complete, corrections=corrections
    )
    return rows[0]["status"]


async def test_a_free_text_answer_becomes_a_correction_the_judge_follows(sf) -> None:
    payload = await _resolve(sf, options=None, answer=ANSWER)
    corrections = founder_corrections(payload)
    assert corrections == [C1, ANSWER]  # 시간순, 나중 것이 뒤
    assert await _judge(AS_Y, corrections) == "done"


async def test_an_option_pick_or_action_key_is_not_a_correction(sf) -> None:
    picked = await _resolve(sf, options=["Postgres", "SQLite"], answer="Postgres")
    assert founder_corrections(picked) == [C1]
    retried = await _resolve(sf, options=None, action_key="acknowledge", kind="merge_watch_stalled")
    assert founder_corrections(retried) == [C1]
