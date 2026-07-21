"""The ``product_tick`` meta-instruction — the run task a product tick seeds.

A ``product_tick`` schedule sets only the cadence (WHEN); BSVibe decides WHAT to
do at fire time. So the emitter cannot carry a founder-written ``text`` — it
seeds THIS localized meta-instruction as the run's task. The agent loop (which
already holds knowledge-search + history tools) reads the product's goal +
metadata + accumulated knowledge + recent run history, decides the single most
valuable next action, and DOES it — or asks via ``ask_user_question`` when the
direction is genuinely ambiguous.

These are SHORT, FIXED strings localized per ``workspaces.language`` — a static
catalog keyed by language, NOT the LLM ``language_directive`` generation path
(same rationale as :mod:`backend.notifications.copy`). An unknown / missing
language falls back to English. Pure leaf module — no DB, no context imports —
so the emitter can seed it with no import-linter edge.
"""

from __future__ import annotations

_DEFAULT_LANGUAGE = "en"

#: The tick meta-instruction per supported language. Prose only — the agent's
#: tools + identifiers stay language-agnostic. Keep EN + KO in lockstep.
_INSTRUCTION: dict[str, str] = {
    "en": (
        "This is an autonomous product tick. Review THIS product's goal and "
        "metadata, its accumulated knowledge, and its recent run history, then "
        "decide the single most valuable NEXT action to advance the product "
        "through its lifecycle — and DO it. Prefer concrete, shippable progress "
        "over analysis. If the direction is genuinely ambiguous, ask the founder "
        "with ask_user_question instead of guessing."
    ),
    "ko": (
        "이건 자율 제품 틱이에요. 이 제품의 목표와 메타데이터, 축적된 지식, 최근 "
        "작업 이력을 검토한 뒤, 제품을 다음 단계로 나아가게 할 가장 가치 있는 단일 "
        "작업을 스스로 정하고 — 실제로 수행하세요. 분석보다 구체적이고 배포 가능한 "
        "진전을 우선하세요. 방향이 정말 애매하면 추측하지 말고 ask_user_question으로 "
        "형님에게 물어보세요."
    ),
}


def product_tick_instruction(language: str | None = None) -> str:
    """The localized tick meta-instruction seeded as a product_tick run's task.

    An unknown / missing ``language`` falls back to English (never empty).
    """
    lang = (language or "").strip() or _DEFAULT_LANGUAGE
    return _INSTRUCTION.get(lang, _INSTRUCTION[_DEFAULT_LANGUAGE])


__all__ = ["product_tick_instruction"]
