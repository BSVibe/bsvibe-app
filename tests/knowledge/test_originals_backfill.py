"""이미 쌓인 원본을 vault 로 내린다 (백필).

배선(#854)은 **앞으로** 발생하는 것만 남긴다. 형님이 말한 "히스토리성"은 과거가
있어야 성립한다 — prod 실측(2026-08-31): 요청 223건 · settle 147건이 DB 에만
있고 vault 에는 0건이다.

⚠️ 이 스위트가 지키는 두 가지

1. **파생 규칙은 한 벌이다.** 백필이 payload 에서 피드백·회고를 뽑는 방식은
   실시간 경로와 **같은 함수**여야 한다. 두 벌이 되는 순간 원클릭 가드(§13)가
   한쪽에서만 지켜지고, 그게 정확히 #823 이 남긴 교훈이다.
2. **과거를 다시 쓰지 않는다.** 백필과 실시간 기록이 같은 행을 건드려도
   ``record_original`` 의 ``O_EXCL`` 이 처음 바이트를 지킨다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.common.settle_kinds import DECISION_RESOLUTION_SETTLE_KIND
from backend.config import get_settings
from backend.knowledge.originals_backfill import backfill_originals
from backend.workflow.application._checkpoint_shared import ACTION_ACKNOWLEDGE
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunActivity, RunStatus
from backend.workflow.infrastructure.intake.db import RequestRow, RequestStatus, TriggerEventRow

from .._support import db_engine

INTENT = "라우팅 규칙이 몇 개인지 한 문장으로만 답해줘.\n조사만 하고 보고해라."
ANSWER = "2번이 맞다 — 조사 보고만으로 충분하다."
INSIGHT = "샌드박스의 editable install 은 믿을 게 못 된다."


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


def _seeds(vault_root: Path, workspace_id: uuid.UUID, kind: str) -> list[Path]:
    d = vault_root / get_settings().knowledge_default_region / str(workspace_id) / "seeds" / kind
    return sorted(d.glob("*.md")) if d.exists() else []


async def _seed_request(sf, *, workspace_id: uuid.UUID, payload: dict) -> uuid.UUID:
    request_id = uuid.uuid4()
    trigger_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            TriggerEventRow(
                id=trigger_id,
                workspace_id=workspace_id,
                trigger_kind="direct",
                source="test",
                idempotency_key=f"k-{trigger_id}",
                payload={},
                received_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            RequestRow(
                id=request_id,
                workspace_id=workspace_id,
                trigger_event_id=trigger_id,
                status=RequestStatus.OPEN,
                payload=payload,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return request_id


async def _seed_settle(sf, *, workspace_id: uuid.UUID, payload: dict) -> uuid.UUID:
    run_id = uuid.uuid4()
    activity_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.REVIEW_READY,
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            ExecutionRunActivity(
                id=activity_id,
                run_id=run_id,
                workspace_id=workspace_id,
                activity_type="settle",
                payload=payload,
            )
        )
        await s.commit()
    return activity_id


class TestThePastIsBroughtDown:
    @pytest.mark.asyncio
    async def test_a_stored_request_becomes_an_original(self, sf, tmp_path: Path) -> None:
        ws = uuid.uuid4()
        request_id = await _seed_request(sf, workspace_id=ws, payload={"text": INTENT})

        async with sf() as s:
            result = await backfill_originals(s, workspace_id=ws, vault_root=tmp_path)

        assert result.recorded == 1
        written = _seeds(tmp_path, ws, "request")
        assert [p.stem for p in written] == [str(request_id)]
        assert INTENT in written[0].read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_a_stored_settle_yields_feedback_and_retrospect(self, sf, tmp_path: Path) -> None:
        ws = uuid.uuid4()
        activity_id = await _seed_settle(
            sf,
            workspace_id=ws,
            payload={
                "verified": True,
                "summary": "s",
                "kind": DECISION_RESOLUTION_SETTLE_KIND,
                "question": "구현으로 갈까요?",
                "answer": ANSWER,
                "agent_knowledge": {"topic": "sandbox", "insight": INSIGHT},
            },
        )

        async with sf() as s:
            result = await backfill_originals(s, workspace_id=ws, vault_root=tmp_path)

        assert result.recorded == 2
        feedback = _seeds(tmp_path, ws, "feedback")
        retrospect = _seeds(tmp_path, ws, "retrospect")
        assert [p.stem for p in feedback] == [str(activity_id)]
        assert [p.stem for p in retrospect] == [str(activity_id)]
        assert ANSWER in feedback[0].read_text(encoding="utf-8")
        assert INSIGHT in retrospect[0].read_text(encoding="utf-8")


class TestTheDerivationRuleIsShared:
    @pytest.mark.asyncio
    async def test_a_one_click_answer_makes_no_feedback_original(self, sf, tmp_path: Path) -> None:
        """§13 이 백필에서도 지켜진다 — 규칙이 두 벌이면 여기서 빨개진다.

        원클릭 액션은 ``answer`` 자리에 **버튼 키**가 들어간다. 백필이 payload 를
        자기 방식으로 읽으면 그 버튼 키로 원본을 만들고, 형님이 한 글자도 안 쓴
        '피드백' 411건이 vault 에 쌓인다.
        """
        ws = uuid.uuid4()
        await _seed_settle(
            sf,
            workspace_id=ws,
            payload={
                "verified": True,
                "summary": "s",
                "kind": DECISION_RESOLUTION_SETTLE_KIND,
                "question": "승인할까요?",
                "answer": ACTION_ACKNOWLEDGE,
                "action_key": ACTION_ACKNOWLEDGE,
            },
        )

        async with sf() as s:
            result = await backfill_originals(s, workspace_id=ws, vault_root=tmp_path)

        assert result.recorded == 0
        assert _seeds(tmp_path, ws, "feedback") == []


class TestBackfillIsIdempotent:
    @pytest.mark.asyncio
    async def test_running_twice_changes_nothing(self, sf, tmp_path: Path) -> None:
        ws = uuid.uuid4()
        await _seed_request(sf, workspace_id=ws, payload={"text": INTENT})

        async with sf() as s:
            first = await backfill_originals(s, workspace_id=ws, vault_root=tmp_path)
        before = _seeds(tmp_path, ws, "request")[0].read_bytes()

        async with sf() as s:
            second = await backfill_originals(s, workspace_id=ws, vault_root=tmp_path)

        assert first.recorded == 1
        assert second.recorded == 0
        assert second.already == 1
        assert len(_seeds(tmp_path, ws, "request")) == 1
        assert _seeds(tmp_path, ws, "request")[0].read_bytes() == before

    @pytest.mark.asyncio
    async def test_it_never_overwrites_what_the_live_path_wrote(self, sf, tmp_path: Path) -> None:
        """실시간 기록이 이미 남긴 원본을 백필이 다시 쓰지 않는다."""
        from backend.knowledge.factory import KnowledgeFactory
        from backend.knowledge.originals import record_original

        ws = uuid.uuid4()
        request_id = await _seed_request(sf, workspace_id=ws, payload={"text": INTENT})
        vault = KnowledgeFactory(workspace_id=str(ws), vault_root=tmp_path).vault()
        await record_original(
            vault=vault,
            kind="request",
            key=str(request_id),
            title="실시간이 먼저 썼다",
            content="실시간이 쓴 본문",
        )

        async with sf() as s:
            await backfill_originals(s, workspace_id=ws, vault_root=tmp_path)

        body = _seeds(tmp_path, ws, "request")[0].read_text(encoding="utf-8")
        assert "실시간이 쓴 본문" in body
        assert INTENT not in body


class TestBackfillIsScopedAndBounded:
    @pytest.mark.asyncio
    async def test_another_workspace_is_untouched(self, sf, tmp_path: Path) -> None:
        """워크스페이스 경계 — 백필은 호출자의 것만 내린다."""
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        await _seed_request(sf, workspace_id=mine, payload={"text": INTENT})
        await _seed_request(sf, workspace_id=theirs, payload={"text": "남의 지시문"})

        async with sf() as s:
            result = await backfill_originals(s, workspace_id=mine, vault_root=tmp_path)

        assert result.recorded == 1
        assert len(_seeds(tmp_path, mine, "request")) == 1
        assert _seeds(tmp_path, theirs, "request") == []

    @pytest.mark.asyncio
    async def test_a_pass_is_bounded_and_reports_remaining(self, sf, tmp_path: Path) -> None:
        """한 번에 다 하지 않는다 — 상한을 두고 ``remaining`` 으로 남은 몫을 알린다.

        HTTP/MCP 표면은 오래 걸리는 작업을 끝까지 못 들고 있다(prod 2026-08-28:
        524 가 성공을 실패로 배달했다). 멱등이므로 상한 + ``remaining`` 이면
        호출자가 다시 부르면 된다 — 잡 테이블이 필요 없다.
        """
        ws = uuid.uuid4()
        for _ in range(3):
            await _seed_request(sf, workspace_id=ws, payload={"text": INTENT})

        async with sf() as s:
            first = await backfill_originals(s, workspace_id=ws, vault_root=tmp_path, limit=2)

        assert first.recorded == 2
        assert first.remaining == 1

        async with sf() as s:
            second = await backfill_originals(s, workspace_id=ws, vault_root=tmp_path, limit=2)

        assert second.recorded == 1
        assert second.remaining == 0
        assert len(_seeds(tmp_path, ws, "request")) == 3

    @pytest.mark.asyncio
    async def test_dry_run_reports_without_writing(self, sf, tmp_path: Path) -> None:
        """건수를 먼저 보고, 쓰기는 그다음 — 형님이 규모를 보고 결정한다."""
        ws = uuid.uuid4()
        await _seed_request(sf, workspace_id=ws, payload={"text": INTENT})

        async with sf() as s:
            result = await backfill_originals(s, workspace_id=ws, vault_root=tmp_path, dry_run=True)

        assert result.pending == 1
        assert result.recorded == 0
        assert _seeds(tmp_path, ws, "request") == []
