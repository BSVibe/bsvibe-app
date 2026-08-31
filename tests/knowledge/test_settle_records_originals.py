"""settle 싱크가 지나가는 원본 셋을 vault 에 남긴다.

:class:`Settlement` 은 세 원본을 **이미 전부** 들고 있다 — ``intent_text``(형님
요청), ``question``/``answer``(피드백), ``agent_knowledge``(회고). 그런데 vault 로
나가던 것은 그중 일부를 **요약·가공한 관찰 노트** 하나뿐이었다. 원본은 DB
운영 테이블에만 남아 ``execution_runs`` 의 ``ON DELETE CASCADE`` 를 탔다.

그래서 없던 것은 서브시스템이 아니라 **연결 하나**다: 싱크가 이미 가진 vault 에
:func:`~backend.knowledge.originals.record_original` 을 부르면 된다.

⚠️ 이 스위트가 지키는 경계 — 원본 기록은 관찰 노트 게이트와 **독립**이다.
게이트를 통과하지 못해 노트가 안 생기는 settlement 도 원본은 남아야 한다
(회고가 없는 평범한 작업이라도 형님의 요청 원문은 히스토리다).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.common.settle_kinds import DECISION_RESOLUTION_SETTLE_KIND
from backend.knowledge.infrastructure.workers.settle_worker import (
    KnowledgeSettleSink,
    Settlement,
)
from backend.workflow.application._checkpoint_shared import ACTION_ACKNOWLEDGE

WORKSPACE = uuid.UUID("5fa3494c-74cf-4d5a-b92b-6054f5684d07")
RUN = uuid.UUID("6b14b269-24eb-4154-b31d-09cb724e83c7")
ACTIVITY = uuid.UUID("71c7a930-7731-4f59-999f-2bc9820ce95a")


def _seeds(tmp_path: Path, kind: str) -> Path:
    """The workspace-scoped ``seeds/<kind>/`` the sink writes under.

    The middle segment is a DEPLOYMENT constant, never a per-workspace value —
    so it is read inline rather than bound to a local of that name, which is
    what ``test_the_workspace_region_axis_is_gone`` forbids.
    """
    from backend.config import get_settings

    return tmp_path / get_settings().knowledge_default_region / str(WORKSPACE) / "seeds" / kind


def _sink(tmp_path: Path) -> KnowledgeSettleSink:
    return KnowledgeSettleSink(vault_root=tmp_path)


def _settlement(**overrides: object) -> Settlement:
    base: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "run_id": RUN,
        "activity_id": ACTIVITY,
        "verified": True,
        "summary": "라우팅 규칙 개수 확인",
    }
    base.update(overrides)
    return Settlement(**base)  # type: ignore[arg-type]


class TestTheRequestOriginalIsKept:
    @pytest.mark.asyncio
    async def test_intent_text_is_recorded_verbatim(self, tmp_path: Path) -> None:
        """형님이 런을 시작시킨 지시문이 글자 그대로 남는다."""
        intent = "라우팅 규칙이 몇 개인지 한 문장으로만 답해줘.\n조사만 하고 보고해라 — 파일은 하나도 쓰지 마라."

        await _sink(tmp_path).absorb(_settlement(intent_text=intent))

        written = list(_seeds(tmp_path, "request").glob("*.md"))
        assert len(written) == 1
        assert written[0].stem == str(RUN)
        assert intent in written[0].read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_request_is_kept_even_when_no_note_is_worth_writing(self, tmp_path: Path) -> None:
        """관찰 노트 게이트를 못 넘는 평범한 작업도 요청 원본은 남는다.

        이 경계가 이 PR 의 요점이다 — 원본 보존은 '기억할 가치' 판단과
        독립이다. 회고를 선언하지 않은 런이 147건 중 137건이었고, 그 137건의
        요청도 전부 히스토리다.
        """
        settlement = _settlement(intent_text="평범한 작업 지시", agent_knowledge=None)

        note = await _sink(tmp_path).absorb(settlement)

        assert note is None, "회고도 형님 텍스트도 없으면 관찰 노트는 안 생긴다"
        assert list(_seeds(tmp_path, "request").glob("*.md")), "그래도 요청 원본은 남아야 한다"


class TestTheFeedbackOriginalIsKept:
    @pytest.mark.asyncio
    async def test_founder_answer_is_recorded(self, tmp_path: Path) -> None:
        answer = "2번이 맞다 — 조사 보고만으로 충분하다. 구현으로 전환하지 마라."

        await _sink(tmp_path).absorb(
            _settlement(
                kind=DECISION_RESOLUTION_SETTLE_KIND,
                question="이 갭들을 실제로 구현해달라는 뜻이었나?",
                answer=answer,
            )
        )

        written = list(_seeds(tmp_path, "feedback").glob("*.md"))
        assert len(written) == 1
        assert answer in written[0].read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_one_click_action_leaves_no_feedback_original(self, tmp_path: Path) -> None:
        """원클릭 승인은 형님이 쓴 글자가 0자다 — 원본이 아니다.

        ``answer`` 자리에 들어 있는 것은 **버튼 키**이고, 그것으로 원본을
        만들면 시스템이 자기 질문을 되읊은 파일이 쌓인다. 판정은
        ``founder_authored_text`` 하나만 쓴다 — 규칙을 두 번 적지 않는다.
        """
        await _sink(tmp_path).absorb(
            _settlement(
                kind=DECISION_RESOLUTION_SETTLE_KIND,
                question="승인할까요?",
                answer=ACTION_ACKNOWLEDGE,
                action_key=ACTION_ACKNOWLEDGE,
            )
        )

        assert not list(_seeds(tmp_path, "feedback").glob("*.md"))


class TestTheRetrospectOriginalIsKept:
    @pytest.mark.asyncio
    async def test_declared_knowledge_is_recorded_verbatim(self, tmp_path: Path) -> None:
        """에이전트가 선언한 회고의 원문이 가공 전 상태로 남는다.

        관찰 노트(``_memory_body``)는 이것을 감싸 가공한다. 원본 레이어는 그
        이전을 보존한다.
        """
        from backend.knowledge.extraction.worth_remembering import RememberableKnowledge

        insight = "샌드박스의 editable install 은 믿을 게 못 된다 — 실행 전에 import 로 확인해라."

        await _sink(tmp_path).absorb(
            _settlement(
                agent_knowledge=RememberableKnowledge(
                    topic="sandbox editable install", insight=insight
                )
            )
        )

        written = list(_seeds(tmp_path, "retrospect").glob("*.md"))
        assert len(written) == 1
        assert insight in written[0].read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_routine_work_records_no_retrospect(self, tmp_path: Path) -> None:
        """선언이 없으면 회고 원본도 없다 — 사후 추출기는 없다."""
        await _sink(tmp_path).absorb(_settlement(intent_text="무언가"))

        assert not list(_seeds(tmp_path, "retrospect").glob("*.md"))


class TestRecordingDoesNotBreakTheSink:
    @pytest.mark.asyncio
    async def test_absorb_still_returns_the_note_path(self, tmp_path: Path) -> None:
        """원본 기록을 얹어도 싱크의 기존 반환 계약이 그대로여야 한다."""
        from backend.knowledge.extraction.worth_remembering import RememberableKnowledge

        note = await _sink(tmp_path).absorb(
            _settlement(
                intent_text="지시문",
                agent_knowledge=RememberableKnowledge(topic="주제", insight="통찰"),
            )
        )

        assert note is not None
        assert "주제" in note or "topic" in note.lower() or note.endswith(".md")
