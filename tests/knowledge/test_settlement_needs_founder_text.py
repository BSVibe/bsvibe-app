"""settlement 은 형님이 **직접 쓴 텍스트**를 담을 때만 지식이 된다 (§13).

``worth_remembering.is_inherently_notable`` 는 두 kind 에 *"LLM 판단 없이 무조건
기억가치 있음"* 을 부여한다. 그 자기 docstring 이 전제를 명시한다 — *"a user decision
or a **discard-with-reason** is knowledge by construction"*. 즉 **형님이 실제로 무언가를
썼다**가 전제다.

그런데 그 규칙을 먹이는 생산자는 둘이고 전제를 지키는 쪽은 하나뿐이었다:

===========================================  ====================  ==========
생산자                                        kind                  전제 강제?
===========================================  ====================  ==========
``SafeModeQueue.deny``                        ``negative_pattern``  ✅
``resolve_checkpoint``                        ``decision_resolution``  ❌
===========================================  ====================  ==========

prod 실측 (2026-08-25): ``decision_resolution`` settle 활동 **11건 → vault 노트 11건**.
그중 자유 텍스트는 5건뿐이고 ``acknowledge`` 4 + ``discard`` 2 = **6건(55%)이 형님이
쓴 글자 0자**다. 원클릭 액션은 ``answer`` 자리에 **버튼 키**가 그대로 들어가서, 노트
본문이 시스템이 자기 질문을 되읊은 것이 된다.

여기서 고치는 것은 ``acknowledge`` 예외가 아니라 **전제가 한 곳에만 있다는 사실**이다.
규칙은 ``backend.common.settle_kinds`` (아무것도 import 하지 않는 leaf) 에 한 번 적히고,
소비자 choke point 인 :class:`KnowledgeSettleSink` 가 그것을 강제한다 — 생산자가 몇 개든
(미래의 세 번째 포함) 같은 규칙 아래 있다.

⚠️ **깨뜨리면 안 되는 것** (양성 대조군, 이 파일 맨 아래 두 테스트):

1. 자유 텍스트 Decision 해소는 **계속** 노트가 된다 (prod 5건).
2. 사유 있는 Safe Mode 거절은 **계속** ``negative_pattern`` 지식이 된다 — 이게
   trust ratchet 의 심장이다(#759/#760).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.common.settle_kinds import (
    DECISION_RESOLUTION_SETTLE_KIND,
    NEGATIVE_PATTERN_SETTLE_KIND,
    founder_authored_text,
)
from backend.config import get_settings
from backend.knowledge.extraction.worth_remembering import is_inherently_notable
from backend.knowledge.infrastructure.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
)
from backend.workflow.application._checkpoint_shared import (
    ACTION_ACKNOWLEDGE,
    ACTION_DISCARD,
)
from backend.workflow.application.checkpoint_resolution import resolve_checkpoint
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
)
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus

from .._support import db_engine

# ── 규칙 그 자체 — 공유 leaf 의 순수 술어 ─────────────────────────────────────


def test_a_button_key_is_not_founder_authored_text() -> None:
    """원클릭 액션은 ``answer`` 자리에 버튼 키가 그대로 들어간다. 형님이 친 글자는 0자다."""
    assert (
        founder_authored_text(answer="acknowledge", reason=None, action_key="acknowledge") is None
    )
    assert founder_authored_text(answer="discard", reason=None, action_key="discard") is None
    assert founder_authored_text(answer="ship", reason=None, action_key="ship") is None
    # 미래에 추가될 원클릭 액션도 이름을 몰라도 걸린다 — allowlist 가 아니라 전제다.
    assert founder_authored_text(answer="snooze", reason=None, action_key="snooze") is None


def test_free_text_is_founder_authored_text() -> None:
    """``action_key`` 없이 온 ``answer`` 는 형님이 직접 친 문장이다."""
    got = founder_authored_text(answer="Postgres 로 가자", reason=None, action_key=None)
    assert got == "Postgres 로 가자"


def test_a_written_reason_is_founder_authored_text() -> None:
    """거절 사유는 형님이 직접 쓴 텍스트 — trust ratchet 의 근거."""
    got = founder_authored_text(
        answer=None, reason="  실제 스택 위에서 못 잡는다  ", action_key=None
    )
    assert got == "실제 스택 위에서 못 잡는다"


def test_blank_and_absent_are_not_founder_authored_text() -> None:
    assert founder_authored_text(answer="   ", reason="", action_key=None) is None
    assert founder_authored_text(answer=None, reason=None, action_key=None) is None


# ── 소비자 게이트 — kind 만으로는 부족하다 ────────────────────────────────────


def test_inherently_notable_requires_founder_text() -> None:
    """kind 는 필요조건일 뿐이다. 전제(형님이 썼다)가 없으면 지식이 아니다."""
    assert is_inherently_notable(DECISION_RESOLUTION_SETTLE_KIND, founder_text="Postgres") is True
    assert is_inherently_notable(DECISION_RESOLUTION_SETTLE_KIND, founder_text=None) is False
    assert (
        is_inherently_notable(NEGATIVE_PATTERN_SETTLE_KIND, founder_text="hard to reason") is True
    )
    assert is_inherently_notable(NEGATIVE_PATTERN_SETTLE_KIND, founder_text=None) is False


def test_plain_verified_work_is_never_inherently_notable() -> None:
    """형님 텍스트가 있어도 검증된 일상 작업은 kind 로 지식이 되지 않는다 —
    에이전트가 계약에서 스스로 선언해야 한다."""
    assert is_inherently_notable(None, founder_text="뭔가 썼다") is False
    assert is_inherently_notable("verified_work", founder_text="뭔가 썼다") is False


# ── 생산자 → 소비자 실경로 ────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


async def _seed_decision(
    sf,
    workspace_id: uuid.UUID,
    *,
    kind: str,
    run_status: RunStatus,
    payload: dict | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """PENDING Decision + 그 런. 반환 ``(run_id, decision_id)``."""
    run_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=run_status,
                payload={"intent_text": "리포트에 기간 표시 추가"},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            Decision(
                id=decision_id,
                run_id=run_id,
                workspace_id=workspace_id,
                decision=kind,
                payload=payload or {"reason": "ci_deadline_exceeded"},
                status=DecisionStatus.PENDING,
            )
        )
        await s.commit()
    return run_id, decision_id


async def _settle_payloads(sf, kind: str) -> list[dict]:
    async with sf() as s:
        rows = (
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.activity_type == "settle"
                    )
                )
            )
            .scalars()
            .all()
        )
    return [r.payload for r in rows if (r.payload or {}).get("kind") == kind]


def _notes(vault_root, workspace_id: uuid.UUID) -> list:
    ws_dir = vault_root / get_settings().knowledge_default_region / str(workspace_id)
    return list(ws_dir.rglob("*.md")) if ws_dir.exists() else []


async def _drain(sf, vault_root) -> int:
    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=vault_root),
        config=SettleWorkerConfig(),
    )
    return await worker.drain_once()


async def test_one_click_acknowledge_leaves_no_vault_note(
    sf, tmp_path, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    """§13 의 결함 그 자체. 버튼 하나로 끝낸 Decision 은 형님이 친 글자가 0자다 —
    노트 본문이 시스템이 자기 질문을 되읊은 것이 되어선 안 된다."""
    _run_id, decision_id = await _seed_decision(
        sf, workspace_id, kind="merge_watch_stalled", run_status=RunStatus.SHIPPED
    )
    async with sf() as s:
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="",
            action_key=ACTION_ACKNOWLEDGE,
            actor_id=founder_id,
        )
        await s.commit()

    # 감사 흔적은 그대로다 — settle 활동은 계속 기록된다(런 히스토리/타임라인).
    payloads = await _settle_payloads(sf, DECISION_RESOLUTION_SETTLE_KIND)
    assert len(payloads) == 1
    assert payloads[0]["action_key"] == ACTION_ACKNOWLEDGE
    assert payloads[0]["answer"] == ACTION_ACKNOWLEDGE  # 버튼 키가 답으로 들어가 있다

    # 그러나 vault 노트는 하나도 안 생긴다.
    assert await _drain(sf, tmp_path) == 1
    assert _notes(tmp_path, workspace_id) == []


async def test_one_click_discard_without_a_reason_leaves_no_vault_note(
    sf, tmp_path, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    """docstring 은 *"discard-with-reason"* 이라 했는데 사유 없는 ``discard`` 도 그냥
    통과했다 (prod 2건). ``acknowledge`` 만 예외 처리하면 이게 남는다."""
    _run_id, decision_id = await _seed_decision(
        sf, workspace_id, kind="run_drive_failed", run_status=RunStatus.RUNNING
    )
    async with sf() as s:
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="",
            action_key=ACTION_DISCARD,
            reason="",
            actor_id=founder_id,
        )
        await s.commit()

    assert len(await _settle_payloads(sf, DECISION_RESOLUTION_SETTLE_KIND)) == 1
    # 사유가 없으니 negative_pattern 행 자체가 없다 (기존 생산자 게이트).
    assert await _settle_payloads(sf, NEGATIVE_PATTERN_SETTLE_KIND) == []

    assert await _drain(sf, tmp_path) == 1
    assert _notes(tmp_path, workspace_id) == []


async def test_discard_with_a_reason_keeps_only_the_founders_own_words(
    sf, tmp_path, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    """사유를 쓴 폐기는 **한 장**만 남는다 — 형님의 문장을 담은 negative_pattern.
    같은 사건의 decision_resolution 행은 답이 버튼 키(``discard``)라 지식이 아니다."""
    _run_id, decision_id = await _seed_decision(
        sf, workspace_id, kind="run_drive_failed", run_status=RunStatus.RUNNING
    )
    async with sf() as s:
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="",
            action_key=ACTION_DISCARD,
            reason="이 접근은 실제 스택 위에서 깨지는 것을 정의상 못 잡는다",
            actor_id=founder_id,
        )
        await s.commit()

    assert len(await _settle_payloads(sf, DECISION_RESOLUTION_SETTLE_KIND)) == 1
    assert len(await _settle_payloads(sf, NEGATIVE_PATTERN_SETTLE_KIND)) == 1

    assert await _drain(sf, tmp_path) == 2
    notes = _notes(tmp_path, workspace_id)
    assert len(notes) == 1
    body = notes[0].read_text(encoding="utf-8")
    assert "실제 스택 위에서 깨지는 것을 정의상 못 잡는다" in body
    # 버튼 키가 답으로 박힌 노트는 없다.
    assert "A: discard" not in body


# ── 양성 대조군 — 이 둘이 빨개지면 과교정이다 ────────────────────────────────


async def test_positive_control_free_text_decision_still_becomes_a_note(
    sf, tmp_path, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    """양성 대조군 1 — prod 의 정당한 5건. 형님이 직접 친 답은 계속 지식이 된다."""
    _run_id, decision_id = await _seed_decision(
        sf,
        workspace_id,
        kind="ask_user_question",
        run_status=RunStatus.RUNNING,
        payload={"question": "큐 저장소는 무엇으로 할까요?"},
    )
    async with sf() as s:
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="SQLite 말고 Postgres 로 간다",
            actor_id=founder_id,
        )
        await s.commit()

    assert await _drain(sf, tmp_path) == 1
    notes = _notes(tmp_path, workspace_id)
    assert len(notes) == 1
    body = notes[0].read_text(encoding="utf-8")
    assert "SQLite 말고 Postgres 로 간다" in body


async def test_positive_control_safe_mode_denial_with_a_reason_still_becomes_knowledge(
    sf, tmp_path, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    """양성 대조군 2 — trust ratchet 의 심장(#759/#760). 형님이 기록한 거절.
    이걸 과교정으로 죽이는 것은 고치려던 버그보다 나쁘다."""
    run_id = uuid.uuid4()
    item_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.RUNNING,
                payload={"intent_text": "브라우저 검증을 붙여라"},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            SafeModeQueueItemRow(
                id=item_id,
                workspace_id=workspace_id,
                deliverable_id=uuid.uuid4(),
                run_id=run_id,
                status=SafeModeStatus.PENDING,
                expires_at=datetime.now(tz=UTC) + timedelta(days=30),
            )
        )
        await s.commit()

    async with sf() as s:
        flipped = await SafeModeQueue(s).deny(
            workspace_id=workspace_id,
            item_id=item_id,
            actor_id=founder_id,
            reason="백엔드를 가로채면 실제 스택 위에서 깨지는 것을 정의상 못 잡는다",
        )
        await s.commit()
    assert flipped is True

    assert len(await _settle_payloads(sf, NEGATIVE_PATTERN_SETTLE_KIND)) == 1
    assert await _drain(sf, tmp_path) == 1
    notes = _notes(tmp_path, workspace_id)
    assert len(notes) == 1
    assert "실제 스택 위에서 깨지는 것을 정의상 못 잡는다" in notes[0].read_text(encoding="utf-8")
