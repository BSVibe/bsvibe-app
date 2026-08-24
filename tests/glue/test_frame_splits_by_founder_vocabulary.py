"""프레이밍 쪼갬 — 사용자의 단계 어휘로만 작업을 나눈다 (PR B).

고정 2값(``single`` / ``design_then_impl``) 예측을 대체한다. 프레이머는 이제
*이 워크스페이스가 어떤 단계를 구분하는가*를 룰에서 받아, 그 어휘 안에서만
작업을 쪼갠다.

불변식:
* 어휘가 비면 **절대 쪼개지 않는다** — 근거 없는 예측은 없다 (fail-closed).
  내장 기본 어휘로 폴백하는 것은 이 변경이 없애려는 그 예측이다.
* 어휘에 없는 단계 이름은 **버린다** — 룰이 없는 단계로 보낸 스텝은 라우팅될
  곳이 없다.
* ASK(``knowledge_only``)는 쪼개지 않는다 — 구현할 것이 없다.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.extensions.skill.loader import SkillLoader
from backend.router.routing.run_routing.chaining import StageTerm
from backend.workflow.application.stages.frame import FrameConfig, FrameStage
from backend.workflow.infrastructure.intake.db import RequestRow, RequestStatus


class _StubFrameLlm:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = json.dumps(response)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def complete_text(self, *, system: str, user: str) -> str:
        self.systems.append(system)
        self.prompts.append(user)
        return self._response


def _request(text: str = "결제 시스템을 새로 만들어줘") -> RequestRow:
    return RequestRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        trigger_event_id=uuid.uuid4(),
        payload={"text": text},
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


_VOCAB = [
    StageTerm(label="design", description="설계처럼 깊이 생각해야 하는 작업"),
    StageTerm(label="impl", description="구현 작업"),
]


def _frame_json(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "framed_intent": "결제 시스템 구축",
        "summary_title": "결제 시스템 구축",
        "skill_match": None,
        "artifact_type_hint": "code",
        "path_classification": "agent_loop",
    }
    base.update(overrides)
    return base


class TestNoVocabularyNeverSplits:
    async def test_empty_vocabulary_yields_no_steps(self, tmp_path: Path) -> None:
        """형님 워크스페이스의 현재 상태 — 룰 0개면 한 런으로 간다."""
        llm = _StubFrameLlm(
            _frame_json(
                steps=[{"stage": "design", "intent": "설계"}, {"stage": "impl", "intent": "구현"}]
            )
        )
        framed = await FrameStage().frame(
            request=_request(),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm, stage_vocabulary=[]),
        )
        assert framed.steps == []

    async def test_the_prompt_does_not_ask_for_steps_without_a_vocabulary(
        self, tmp_path: Path
    ) -> None:
        """어휘가 없으면 모델에게 쪼개라고 묻지도 않는다 — 물으면 답이 나오고,
        나온 답은 버려도 이미 토큰을 썼다."""
        llm = _StubFrameLlm(_frame_json())
        await FrameStage().frame(
            request=_request(),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm, stage_vocabulary=[]),
        )
        assert "steps" not in llm.systems[0]


class TestSplitUsesTheFoundersWords:
    async def test_steps_are_kept_when_every_stage_is_in_the_vocabulary(
        self, tmp_path: Path
    ) -> None:
        llm = _StubFrameLlm(
            _frame_json(
                steps=[
                    {"stage": "design", "intent": "결제 흐름과 실패 처리를 설계한다"},
                    {"stage": "impl", "intent": "설계대로 결제 엔드포인트를 구현한다"},
                ]
            )
        )
        framed = await FrameStage().frame(
            request=_request(),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm, stage_vocabulary=_VOCAB),
        )
        assert [(s.stage, s.intent) for s in framed.steps] == [
            ("design", "결제 흐름과 실패 처리를 설계한다"),
            ("impl", "설계대로 결제 엔드포인트를 구현한다"),
        ]

    async def test_the_vocabulary_and_its_descriptions_reach_the_model(
        self, tmp_path: Path
    ) -> None:
        """프레이머가 읽는 것은 컴파일된 조건이 아니라 형님이 쓴 문장이다."""
        llm = _StubFrameLlm(_frame_json(steps=[{"stage": "impl", "intent": "구현"}]))
        await FrameStage().frame(
            request=_request(),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm, stage_vocabulary=_VOCAB),
        )
        prompt = llm.prompts[0]
        assert "design" in prompt
        assert "설계처럼 깊이 생각해야 하는 작업" in prompt

    async def test_a_single_step_is_not_a_split(self, tmp_path: Path) -> None:
        """한 스텝은 체이닝이 아니다 — 오늘과 같은 한 런."""
        llm = _StubFrameLlm(_frame_json(steps=[{"stage": "impl", "intent": "구현"}]))
        framed = await FrameStage().frame(
            request=_request(),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm, stage_vocabulary=_VOCAB),
        )
        assert [s.stage for s in framed.steps] == ["impl"]


class TestHallucinatedAndIncoherentSplits:
    async def test_a_stage_outside_the_vocabulary_drops_the_whole_split(
        self, tmp_path: Path
    ) -> None:
        """음성 대조군 — 룰 없는 단계로 보낸 스텝은 라우팅될 곳이 없다.
        일부만 버리면 형님이 요청한 작업의 일부가 조용히 사라진다."""
        llm = _StubFrameLlm(
            _frame_json(
                steps=[
                    {"stage": "design", "intent": "설계"},
                    {"stage": "qa", "intent": "테스트"},  # 어휘에 없다
                ]
            )
        )
        framed = await FrameStage().frame(
            request=_request(),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm, stage_vocabulary=_VOCAB),
        )
        assert framed.steps == []

    async def test_a_step_without_an_intent_drops_the_whole_split(self, tmp_path: Path) -> None:
        llm = _StubFrameLlm(
            _frame_json(steps=[{"stage": "design"}, {"stage": "impl", "intent": "구현"}])
        )
        framed = await FrameStage().frame(
            request=_request(),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm, stage_vocabulary=_VOCAB),
        )
        assert framed.steps == []

    async def test_an_ask_is_never_split(self, tmp_path: Path) -> None:
        """ASK 는 구현할 것이 없다 — 어휘가 있어도 쪼개지 않는다."""
        llm = _StubFrameLlm(
            _frame_json(
                artifact_type_hint=None,
                path_classification="knowledge_only",
                steps=[{"stage": "design", "intent": "설계"}, {"stage": "impl", "intent": "구현"}],
            )
        )
        framed = await FrameStage().frame(
            request=_request("라우팅이 어떻게 도는지 설명해줘"),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm, stage_vocabulary=_VOCAB),
        )
        assert framed.steps == []

    async def test_a_malformed_steps_value_is_tolerated(self, tmp_path: Path) -> None:
        llm = _StubFrameLlm(_frame_json(steps="design, then impl"))
        framed = await FrameStage().frame(
            request=_request(),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm, stage_vocabulary=_VOCAB),
        )
        assert framed.steps == []


class TestThePredictionIsGone:
    def test_the_fixed_pipeline_vocabulary_no_longer_exists(self) -> None:
        """양성 대조군의 반대 — 옛 축이 정말 사라졌는지 본다."""
        from backend.workflow.application.stages import frame as frame_mod

        assert not hasattr(frame_mod, "PipelineKind")
        assert not hasattr(frame_mod, "_BUILD_INTENT_WORDS")
        assert not hasattr(frame_mod, "_derive_pipeline")

    def test_pipeline_is_no_longer_a_routable_field(self) -> None:
        from backend.router.routing.run_routing.engine import ALLOWED_FIELDS

        assert "pipeline" not in ALLOWED_FIELDS
        # 양성 대조군 — ``stage`` 는 남는다. 어휘만 사용자 것이 됐다.
        assert "stage" in ALLOWED_FIELDS


class TestTheSplitIsBounded:
    async def test_an_implausibly_long_plan_is_not_a_split(self, tmp_path: Path) -> None:
        """스텝 하나가 곧 런 하나다 — 실행기 턴·워크트리·리뷰 게이트 하나씩.
        모델이 20항목짜리 프로젝트 계획을 내면 아무도 보기 전에 그걸 다 쓴다."""
        llm = _StubFrameLlm(
            _frame_json(steps=[{"stage": "impl", "intent": f"단계 {i}"} for i in range(20)])
        )
        framed = await FrameStage().frame(
            request=_request(),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm, stage_vocabulary=_VOCAB),
        )
        assert framed.steps == []

    async def test_a_stage_may_repeat_within_the_cap(self, tmp_path: Path) -> None:
        """양성 대조군 — 상한은 개수만 본다. 같은 단계를 두 번 쓰는 건 정상이다."""
        llm = _StubFrameLlm(
            _frame_json(
                steps=[
                    {"stage": "design", "intent": "설계"},
                    {"stage": "impl", "intent": "구현"},
                    {"stage": "impl", "intent": "마무리 구현"},
                ]
            )
        )
        framed = await FrameStage().frame(
            request=_request(),
            config=FrameConfig(skill_loader=_loader(tmp_path), llm=llm, stage_vocabulary=_VOCAB),
        )
        assert [s.stage for s in framed.steps] == ["design", "impl", "impl"]
