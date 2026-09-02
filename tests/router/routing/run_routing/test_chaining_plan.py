"""종합 — 사용자의 라우팅 룰에서 단계 어휘를 도출한다 (PR B).

형님 (2026-08-24):
    *"사용자 별로 라우팅 룰들을 종합해서 먼저 통합 된 동적 체이닝 규칙을 만들어야 해.
    그러면 프레이밍 단계에서 그 규칙을 기반으로 작업을 쪼개고, 할당할 수 있겠지"*

핵심 불변식 — **룰이 없으면 쪼갤 근거도 없다.** 고정 어휘
(``single`` / ``design_then_impl``)로 프레임이 미리 맞히던 예측은 사라지고,
단계 이름은 사용자가 자기 룰에 쓴 말에서 나온다.

prod 실측 2026-08-24: 형님이 실제로 쓰는 워크스페이스(`5fa3494c`, 런 169건)의
라우팅 룰은 **0개**다. 그 상태에서 프레이머는 32건을 ``design_then_impl`` 로
표시해 런을 둘로 갈랐고, 기록된 라우팅 판정 21건은 **전부**
``workspace_default`` 였다 — 즉 쪼갠 이득이 **0** 이었다.
"""

from __future__ import annotations

import uuid

from backend.router.routing.run_routing.chaining import (
    StageTerm,
    derive_stage_vocabulary,
)
from backend.router.routing.run_routing.db import RunRoutingRuleRow


def _rule(
    *,
    name: str,
    conditions: list[dict[str, object]],
    target: str = "sonnet",
    source_text: str | None = None,
    is_active: bool = True,
    is_default: bool = False,
    priority: int = 10,
) -> RunRoutingRuleRow:
    return RunRoutingRuleRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        name=name,
        caller_id=None,
        priority=priority,
        is_default=is_default,
        target=target,
        source_text=source_text,
        conditions=conditions,
        is_active=is_active,
    )


class TestNoRulesMeansNoSplit:
    """형님 워크스페이스의 현재 상태 — 근거가 없으면 예측도 없다."""

    def test_empty_rule_set_yields_no_vocabulary(self) -> None:
        assert derive_stage_vocabulary([]) == []

    def test_rules_that_do_not_key_on_stage_yield_no_vocabulary(self) -> None:
        """양성 대조군 — 다른 축의 룰은 체이닝과 무관하다."""
        rules = [
            _rule(
                name="한국어는 sonnet",
                conditions=[{"field": "detected_language", "operator": "eq", "value": "ko"}],
            ),
            _rule(name="기본", conditions=[], is_default=True, priority=100),
        ]
        assert derive_stage_vocabulary(rules) == []


class TestVocabularyComesFromTheFoundersRules:
    def test_stage_labels_are_read_from_stage_conditions(self) -> None:
        """prod admin 워크스페이스의 형님 룰 2개가 그대로 어휘가 된다."""
        rules = [
            _rule(
                name="design stage → claude opus",
                conditions=[{"field": "stage", "operator": "eq", "value": "design"}],
                target="opus",
            ),
            _rule(
                name="impl stage → claude sonnet",
                conditions=[{"field": "stage", "operator": "eq", "value": "impl"}],
            ),
            _rule(name="default → claude sonnet", conditions=[], is_default=True, priority=100),
        ]
        assert derive_stage_vocabulary(rules) == [
            StageTerm(label="design"),
            StageTerm(label="impl"),
        ]

    def test_no_rule_prose_enters_the_vocabulary(self) -> None:
        """룰의 산문은 어휘에 안 들어간다 — ``name`` 도, ``source_text`` 도.
        둘 다 라우팅 **타겟**을 나르기 때문이다(`source_text` 는 조건이 컴파일돼
        나온 원문이므로 필연적으로 모델을 지목한다). 형님이 그 단계를 부르는 말은
        라벨 그 자체다."""
        rules = [
            _rule(
                name="설계 단계는 opus로",
                conditions=[{"field": "stage", "operator": "eq", "value": "design"}],
                source_text="설계 단계는 opus 로 보내라",
            )
        ]
        assert derive_stage_vocabulary(rules) == [StageTerm(label="design")]

    def test_priority_order_is_preserved_and_duplicates_collapse(self) -> None:
        rules = [
            _rule(
                name="late",
                priority=50,
                conditions=[{"field": "stage", "operator": "eq", "value": "review"}],
            ),
            _rule(
                name="early",
                priority=1,
                conditions=[{"field": "stage", "operator": "eq", "value": "design"}],
            ),
            _rule(
                name="dup",
                priority=60,
                conditions=[{"field": "stage", "operator": "eq", "value": "design"}],
            ),
        ]
        assert [t.label for t in derive_stage_vocabulary(rules)] == ["design", "review"]

    def test_an_in_operator_contributes_every_label(self) -> None:
        rules = [
            _rule(
                name="둘 다 opus",
                conditions=[{"field": "stage", "operator": "in", "value": ["design", "review"]}],
                target="opus",
            )
        ]
        assert [t.label for t in derive_stage_vocabulary(rules)] == ["design", "review"]


class TestInactiveAndMalformedRules:
    def test_inactive_rules_contribute_nothing(self) -> None:
        """음성 대조군 — 형님이 끈 룰은 어휘에서도 빠진다."""
        rules = [
            _rule(
                name="꺼진 룰",
                conditions=[{"field": "stage", "operator": "eq", "value": "design"}],
                is_active=False,
            )
        ]
        assert derive_stage_vocabulary(rules) == []

    def test_odd_condition_shapes_are_tolerated(self) -> None:
        rules = [
            _rule(name="a", conditions=[{"field": "stage", "operator": "eq"}]),
            _rule(name="b", conditions=[{"field": "stage", "operator": "eq", "value": ""}]),
            _rule(name="c", conditions=[{"field": "stage", "operator": "eq", "value": 7}]),
            _rule(
                name="d",
                conditions=[{"field": "stage", "operator": "regex", "value": ".*"}],
            ),
        ]
        assert derive_stage_vocabulary(rules) == []

    def test_a_negated_stage_condition_is_not_a_stage_the_work_can_be_in(self) -> None:
        """``stage != design`` 은 '이런 단계가 있다'는 뜻이 아니다."""
        rules = [
            _rule(
                name="설계가 아닌 것",
                conditions=[
                    {"field": "stage", "operator": "eq", "value": "design", "negate": True}
                ],
            )
        ]
        assert derive_stage_vocabulary(rules) == []


class TestTheVocabularyIsDeterministic:
    """prod 의 두 stage 룰은 ``priority`` 가 같다 — tiebreak 이 없으면 DB 가 돌려준
    행 순서가 그대로 프롬프트 순서가 되고, 같은 룰인데 런마다 다른 프롬프트가 된다."""

    def test_same_priority_rules_always_produce_the_same_order(self) -> None:
        design = _rule(
            name="design stage → claude opus",
            priority=10,
            conditions=[{"field": "stage", "operator": "eq", "value": "design"}],
            target="opus",
        )
        impl = _rule(
            name="impl stage → claude sonnet",
            priority=10,
            conditions=[{"field": "stage", "operator": "eq", "value": "impl"}],
        )
        forward = [t.label for t in derive_stage_vocabulary([design, impl])]
        reverse = [t.label for t in derive_stage_vocabulary([impl, design])]
        assert forward == reverse
