"""Built-in preset templates.

Code-defined; no DB representation. Templates carry intent definitions
(with Korean example phrases — sample data the operator edits after
applying) + routing rules referencing abstract target levels.
"""

from __future__ import annotations

from backend.gateway.presets.models import (
    PresetCondition,
    PresetIntent,
    PresetRule,
    PresetTemplate,
)


def get_builtin_presets() -> list[PresetTemplate]:
    return [
        _coding_assistant(),
        _customer_support(),
        _translation_summary(),
        _general(),
    ]


class PresetRegistry:
    """Lookup table for built-in templates."""

    def __init__(self) -> None:
        self._presets: dict[str, PresetTemplate] = {p.name: p for p in get_builtin_presets()}

    def get(self, name: str) -> PresetTemplate | None:
        return self._presets.get(name)

    def list_all(self) -> list[PresetTemplate]:
        return list(self._presets.values())


# ---------------------------------------------------------------------------
# Built-in templates
# ---------------------------------------------------------------------------


def _coding_assistant() -> PresetTemplate:
    return PresetTemplate(
        name="coding-assistant",
        description="코드 생성, 리뷰, 디버깅에 최적화",
        intents=(
            PresetIntent(
                name="code_generation",
                description="코드 생성 요청",
                examples=[
                    "함수 만들어줘",
                    "이 API 구현해줘",
                    "React 컴포넌트 작성해줘",
                ],
            ),
            PresetIntent(
                name="code_review",
                description="코드 리뷰 및 개선",
                examples=[
                    "이 코드 리뷰해줘",
                    "버그 있는지 봐줘",
                    "개선점 알려줘",
                ],
            ),
            PresetIntent(
                name="debugging",
                description="에러 해결 및 디버깅",
                examples=[
                    "에러 나는데 도와줘",
                    "Traceback 분석해줘",
                    "이 버그 원인이 뭐야",
                ],
            ),
            PresetIntent(
                name="documentation",
                description="문서 작성",
                examples=[
                    "README 작성해줘",
                    "docstring 추가해줘",
                ],
            ),
        ),
        rules=(
            PresetRule(
                name="complex-code-tasks",
                target_level="premium",
                conditions=[
                    PresetCondition(
                        condition_type="intent",
                        field="classified_intent",
                        operator="in",
                        value=["code_review", "debugging"],
                    ),
                    PresetCondition(
                        condition_type="token_count",
                        field="estimated_tokens",
                        operator="gt",
                        value=2000,
                    ),
                ],
            ),
            PresetRule(
                name="simple-docs",
                target_level="economy",
                conditions=[
                    PresetCondition(
                        condition_type="intent",
                        field="classified_intent",
                        operator="eq",
                        value="documentation",
                    ),
                ],
            ),
            PresetRule(
                name="default",
                target_level="balanced",
                is_default=True,
            ),
        ),
    )


def _customer_support() -> PresetTemplate:
    return PresetTemplate(
        name="customer-support",
        description="고객 문의 응대에 최적화",
        intents=(
            PresetIntent(
                name="complaint",
                description="불만 및 환불 요청",
                examples=[
                    "환불해주세요",
                    "불만이 있어요",
                    "돈 돌려줘",
                ],
            ),
            PresetIntent(
                name="inquiry",
                description="일반 문의",
                examples=[
                    "영업시간 알려주세요",
                    "가격이 얼마예요",
                ],
            ),
            PresetIntent(
                name="technical_support",
                description="기술 지원",
                examples=[
                    "로그인이 안 돼요",
                    "에러가 나요",
                ],
            ),
        ),
        rules=(
            PresetRule(
                name="complaints-premium",
                target_level="premium",
                conditions=[
                    PresetCondition(
                        condition_type="intent",
                        field="classified_intent",
                        operator="eq",
                        value="complaint",
                    ),
                ],
            ),
            PresetRule(
                name="simple-inquiry",
                target_level="economy",
                conditions=[
                    PresetCondition(
                        condition_type="intent",
                        field="classified_intent",
                        operator="eq",
                        value="inquiry",
                    ),
                ],
            ),
            PresetRule(
                name="default",
                target_level="balanced",
                is_default=True,
            ),
        ),
    )


def _translation_summary() -> PresetTemplate:
    return PresetTemplate(
        name="translation-summary",
        description="다국어 번역 및 문서 요약에 최적화",
        intents=(
            PresetIntent(
                name="translation",
                description="번역 요청",
                examples=[
                    "영어로 번역해줘",
                    "이거 한국어로",
                ],
            ),
            PresetIntent(
                name="summarization",
                description="요약 요청",
                examples=[
                    "이 문서 요약해줘",
                    "핵심만 정리해줘",
                ],
            ),
            PresetIntent(
                name="rewriting",
                description="문장 다듬기",
                examples=[
                    "이 문장 다듬어줘",
                    "더 자연스럽게 바꿔줘",
                ],
            ),
        ),
        rules=(
            PresetRule(
                name="long-document",
                target_level="premium",
                conditions=[
                    PresetCondition(
                        condition_type="token_count",
                        field="estimated_tokens",
                        operator="gt",
                        value=5000,
                    ),
                ],
            ),
            PresetRule(
                name="default",
                target_level="economy",
                is_default=True,
            ),
        ),
    )


def _general() -> PresetTemplate:
    return PresetTemplate(
        name="general",
        description="다양한 용도에 균형 잡힌 라우팅",
        intents=(),
        rules=(
            PresetRule(
                name="complex-requests",
                target_level="premium",
                conditions=[
                    PresetCondition(
                        condition_type="token_count",
                        field="estimated_tokens",
                        operator="gt",
                        value=3000,
                    ),
                ],
            ),
            PresetRule(
                name="default",
                target_level="balanced",
                is_default=True,
            ),
        ),
    )
