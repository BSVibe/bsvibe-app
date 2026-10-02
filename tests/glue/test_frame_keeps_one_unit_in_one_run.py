"""#1103 — 프레이머가 한 단위의 일을 두 런으로 쪼개지 않는다.

실측 2026-09-30: 이슈 #1102 → 런 ``92b76fba``. 프레이머가 본문의 번호 목록을 따라
``frame.steps`` 를 **둘 다 ``implement``** 인 두 스텝으로 만들었다 — "경합을 재현하는
테스트 + 빨강 확인", 그다음 "``transition()`` 을 compare-and-set 으로 수정". 첫 런의
Claude Code 는 지시대로 테스트만 쓰고 끝냈다(입력 3,055,575 토큰). 두 번째 세션은 같은
탐색을 처음부터 다시 해야 했고, 첫 스텝의 산출물은 빨간 테스트만 있는 PR 이었다.

쪼갬이 사는 것은 **라우팅** 하나다 — 스텝마다 다른 단계 이름을 달아 형님의 룰이 다른
모델을 고르게 하는 것. 연속한 두 스텝이 같은 단계면 같은 모델로 가므로 쪼개서 얻는 게
없고, 새 세션이 탐색을 반복하는 비용만 남는다. 그래서 프레이머는 그런 스텝을 하나로
합친다. 결정적인 규칙이라 모델이 번호 목록을 어떻게 읽든 상관없다.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.extensions.skill.loader import SkillLoader
from backend.router.routing.run_routing.chaining import StageTerm
from backend.workflow.application._loop_context import _intent_directive
from backend.workflow.application.stages.frame import (
    FrameConfig,
    FrameStage,
    TextCompletion,
)
from backend.workflow.infrastructure.intake.db import RequestRow, RequestStatus

#: Prod's vocabulary as of 2026-09-02 (see test_frame_splits_by_founder_vocabulary).
_VOCAB = [StageTerm(label="design"), StageTerm(label="implement")]


class _StubFrameLlm:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = json.dumps(response)
        self.systems: list[str] = []

    async def complete_text(self, *, system: str, user: str) -> TextCompletion:
        self.systems.append(system)
        return TextCompletion(text=self._response)


def _request() -> RequestRow:
    return RequestRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        trigger_event_id=uuid.uuid4(),
        payload={"text": "취소한 런이 review_ready 로 덮어써진다"},
        status=RequestStatus.OPEN,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


def _loader(tmp_path: Path) -> SkillLoader:
    root = tmp_path / "skills"
    root.mkdir(parents=True, exist_ok=True)
    loader = SkillLoader(root)
    loader.load_all()
    return loader


async def _frame(tmp_path: Path, steps: list[dict[str, str]]) -> Any:
    llm = _StubFrameLlm(
        {
            "framed_intent": "취소 경합 수정",
            "summary_title": "취소 경합 수정",
            "skill_match": None,
            "artifact_type_hint": "code",
            "path_classification": "agent_loop",
            "steps": steps,
        }
    )
    framed = await FrameStage().frame(
        request=_request(),
        config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm, stage_vocabulary=_VOCAB),
    )
    return framed, llm


#: The split run 92b76fba actually received.
_TEST_THEN_FIX = [
    {"stage": "implement", "intent": "경합을 재현하는 테스트를 작성하고 빨강을 확인한다"},
    {"stage": "implement", "intent": "transition() 을 compare-and-set 으로 수정한다"},
]


class TestConsecutiveSameStageStepsAreOneRun:
    async def test_the_92b76fba_split_becomes_one_step(self, tmp_path: Path) -> None:
        framed, _ = await _frame(tmp_path, _TEST_THEN_FIX)
        assert [s.stage for s in framed.steps] == ["implement"]

    async def test_the_merged_step_keeps_every_part_in_order(self, tmp_path: Path) -> None:
        """합치면서 일을 버리면 안 된다 — 수정이 빠지면 같은 결함(빨간 테스트만)이 남는다."""
        framed, _ = await _frame(tmp_path, _TEST_THEN_FIX)
        intent = framed.steps[0].intent
        test_at = intent.find("테스트를 작성")
        fix_at = intent.find("compare-and-set")
        assert test_at != -1
        assert fix_at != -1
        assert test_at < fix_at

    async def test_only_adjacent_steps_merge(self, tmp_path: Path) -> None:
        """다른 단계가 사이에 있으면 그 스텝은 그 단계의 산출물이 필요해서 뒤에 있다."""
        framed, _ = await _frame(
            tmp_path,
            [
                {"stage": "implement", "intent": "스캐폴드"},
                {"stage": "design", "intent": "설계"},
                {"stage": "implement", "intent": "설계대로 구현"},
            ],
        )
        assert [s.stage for s in framed.steps] == ["implement", "design", "implement"]

    async def test_a_split_across_stages_is_kept(self, tmp_path: Path) -> None:
        """양성 대조군 — 라우팅이 갈리는 쪼갬은 그대로 둔다."""
        framed, _ = await _frame(
            tmp_path,
            [
                {"stage": "design", "intent": "설계"},
                {"stage": "implement", "intent": "구현"},
                {"stage": "implement", "intent": "마무리"},
            ],
        )
        assert [s.stage for s in framed.steps] == ["design", "implement"]
        assert "구현" in framed.steps[1].intent
        assert "마무리" in framed.steps[1].intent


class TestTheFramerIsToldATestAndItsFixAreOneStep:
    async def test_the_split_instruction_names_the_tdd_unit(self, tmp_path: Path) -> None:
        """다른 단계 이름(``test`` / ``implement``)으로 쪼개는 경우는 합칠 수 없으니
        프롬프트가 막아야 한다."""
        _, llm = await _frame(tmp_path, _TEST_THEN_FIX)
        system = llm.systems[0]
        assert "failing test" in system
        assert "the fix that makes it pass" in system


class _Run:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


class TestAOneStepPlanIsNotScopedDown:
    def test_a_single_step_plan_gets_no_one_step_scope_line(self) -> None:
        """한 스텝짜리 계획에서 "This run is ONE step of that request" 는 거짓이다.
        에이전트는 그 문장을 읽고 일부만 하고 멈춘다(92b76fba 의 출력: "다음 단계(이번
        런의 범위 밖)")."""
        run = _Run(
            {
                "intent_text": "취소 경합을 고쳐줘",
                "step_intent": "테스트를 쓰고 고친다",
                "step_index": 0,
                "frame": {"steps": [{"stage": "implement", "intent": "테스트를 쓰고 고친다"}]},
            }
        )
        assert "ONE step" not in _intent_directive(run)

    def test_a_real_split_still_scopes_each_step(self) -> None:
        """양성 대조군 — 진짜 쪼갬에서는 스코프 줄이 있어야 각 런이 전체를 하지 않는다."""
        run = _Run(
            {
                "intent_text": "결제를 만들어줘",
                "step_intent": "설계",
                "step_index": 0,
                "frame": {
                    "steps": [
                        {"stage": "design", "intent": "설계"},
                        {"stage": "implement", "intent": "구현"},
                    ]
                },
            }
        )
        directive = _intent_directive(run)
        assert "ONE step" in directive
        assert "설계" in directive
