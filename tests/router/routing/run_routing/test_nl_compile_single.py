"""Single-condition compiler (NL-native routing Lift N5).

The founder authors ONE rule with a free-text CONDITION ("복잡한 작업", "마케팅
관련", "한국어 요청") + a target model. ``compile_source_text`` turns that ONE
phrase into ONE structured result for a single dimension — reusing N3's dimension
detection + the engine whitelist. The LLM is stubbed; every field is validated.

On nothing-valid the compiler returns an UNINTERPRETABLE signal (not a raise, not
a silent empty rule) so the endpoint can 422 rather than persist a dead rule.
"""

from __future__ import annotations

import json

import pytest

from backend.router.routing.run_routing.nl_compile import (
    CompiledCondition,
    UninterpretableCondition,
    compile_source_text,
)

CALLERS = [
    ("workflow.agent_loop.plan", "design step"),
    ("workflow.agent_loop.act", "implementation step"),
    ("workflow.judge", "verifier"),
]


class _StubLlm:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[tuple[str, str]] = []

    async def complete_text(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self._reply


class _RaisingLlm:
    async def complete_text(self, *, system: str, user: str) -> str:
        raise RuntimeError("model down")


async def _compile(reply: str, text: str = "복잡한 작업"):
    return await compile_source_text(text, callers=CALLERS, llm=_StubLlm(reply))


# ---------------------------------------------------------------------------
# Category dimension → intent def + classified_intent condition
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_category_condition_compiles() -> None:
    reply = json.dumps(
        {
            "intent_name": "marketing",
            "intent_examples": [
                "write a marketing email",
                "draft a landing page headline",
                "plan a launch campaign",
            ],
        }
    )
    result = await compile_source_text("마케팅 관련", callers=CALLERS, llm=_StubLlm(reply))
    assert isinstance(result, CompiledCondition)
    assert result.intent_name == "marketing"
    assert result.intent_examples == [
        "write a marketing email",
        "draft a landing page headline",
        "plan a launch campaign",
    ]
    assert result.condition == {
        "field": "classified_intent",
        "operator": "eq",
        "value": "marketing",
    }
    assert result.caller_id is None


@pytest.mark.asyncio
async def test_category_value_forced_from_intent_name() -> None:
    """A model-supplied condition is ignored; the rule keys on the intent name."""
    reply = json.dumps(
        {
            "intent_name": "design_work",
            "intent_examples": ["design a logo", "create a UI mockup", "pick a palette"],
            "condition": {"field": "classified_intent", "operator": "eq", "value": "WRONG"},
        }
    )
    result = await compile_source_text("디자인 작업", callers=CALLERS, llm=_StubLlm(reply))
    assert isinstance(result, CompiledCondition)
    assert result.condition == {
        "field": "classified_intent",
        "operator": "eq",
        "value": "design_work",
    }


@pytest.mark.asyncio
async def test_category_without_examples_is_uninterpretable() -> None:
    reply = json.dumps({"intent_name": "marketing", "intent_examples": []})
    result = await compile_source_text("마케팅 관련", callers=CALLERS, llm=_StubLlm(reply))
    assert isinstance(result, UninterpretableCondition)


# ---------------------------------------------------------------------------
# Complexity dimension → estimated_tokens / pipeline
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_complexity_estimated_tokens_compiles() -> None:
    reply = json.dumps(
        {"condition": {"field": "estimated_tokens", "operator": "gt", "value": 2000}}
    )
    result = await _compile(reply, text="복잡한 작업")
    assert isinstance(result, CompiledCondition)
    assert result.condition == {"field": "estimated_tokens", "operator": "gt", "value": 2000}
    assert result.intent_name is None
    assert result.caller_id is None


@pytest.mark.asyncio
async def test_complexity_pipeline_compiles() -> None:
    reply = json.dumps(
        {"condition": {"field": "pipeline", "operator": "eq", "value": "design_then_impl"}}
    )
    result = await _compile(reply, text="큰 작업")
    assert isinstance(result, CompiledCondition)
    assert result.condition == {
        "field": "pipeline",
        "operator": "eq",
        "value": "design_then_impl",
    }


# ---------------------------------------------------------------------------
# Language / artifact / stage dimensions
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_language_condition_compiles() -> None:
    reply = json.dumps(
        {"condition": {"field": "detected_language", "operator": "eq", "value": "ko"}}
    )
    result = await compile_source_text("한국어 요청", callers=CALLERS, llm=_StubLlm(reply))
    assert isinstance(result, CompiledCondition)
    assert result.condition == {"field": "detected_language", "operator": "eq", "value": "ko"}


@pytest.mark.asyncio
async def test_artifact_condition_compiles() -> None:
    reply = json.dumps(
        {"condition": {"field": "artifact_type_hint", "operator": "eq", "value": "code"}}
    )
    result = await compile_source_text("코드 작업", callers=CALLERS, llm=_StubLlm(reply))
    assert isinstance(result, CompiledCondition)
    assert result.condition == {"field": "artifact_type_hint", "operator": "eq", "value": "code"}


@pytest.mark.asyncio
async def test_stage_caller_condition_compiles() -> None:
    reply = json.dumps({"caller_id": "workflow.agent_loop.plan"})
    result = await compile_source_text("설계 단계", callers=CALLERS, llm=_StubLlm(reply))
    assert isinstance(result, CompiledCondition)
    assert result.caller_id == "workflow.agent_loop.plan"
    assert result.condition is None
    assert result.intent_name is None


# ---------------------------------------------------------------------------
# Uninterpretable / validation-drop → UninterpretableCondition
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unknown_field_is_uninterpretable() -> None:
    reply = json.dumps({"condition": {"field": "made_up", "operator": "eq", "value": "x"}})
    assert isinstance(await _compile(reply), UninterpretableCondition)


@pytest.mark.asyncio
async def test_unknown_operator_is_uninterpretable() -> None:
    reply = json.dumps(
        {"condition": {"field": "estimated_tokens", "operator": "approx", "value": 1}}
    )
    assert isinstance(await _compile(reply), UninterpretableCondition)


@pytest.mark.asyncio
async def test_unknown_caller_is_uninterpretable() -> None:
    reply = json.dumps({"caller_id": "made.up"})
    assert isinstance(await _compile(reply), UninterpretableCondition)


@pytest.mark.asyncio
async def test_empty_dimension_is_uninterpretable() -> None:
    reply = json.dumps({})
    assert isinstance(await _compile(reply), UninterpretableCondition)


@pytest.mark.asyncio
async def test_empty_text_is_uninterpretable() -> None:
    result = await compile_source_text("  ", callers=CALLERS, llm=_StubLlm("{}"))
    assert isinstance(result, UninterpretableCondition)


@pytest.mark.asyncio
async def test_llm_failure_is_uninterpretable() -> None:
    result = await compile_source_text("복잡한 작업", callers=CALLERS, llm=_RaisingLlm())
    assert isinstance(result, UninterpretableCondition)


@pytest.mark.asyncio
async def test_unparseable_is_uninterpretable() -> None:
    result = await compile_source_text(
        "복잡한 작업", callers=CALLERS, llm=_StubLlm("sorry, no idea")
    )
    assert isinstance(result, UninterpretableCondition)


@pytest.mark.asyncio
async def test_tolerates_code_fence_and_single_object() -> None:
    reply = '```json\n{"condition":{"field":"detected_language","operator":"eq","value":"en"}}\n```'
    result = await _compile(reply, text="english requests")
    assert isinstance(result, CompiledCondition)
    assert result.condition == {"field": "detected_language", "operator": "eq", "value": "en"}


@pytest.mark.asyncio
async def test_tolerates_array_wrapper_takes_first_valid() -> None:
    """Model may wrap the single object in an array — take the first valid one."""
    reply = json.dumps(
        [{"condition": {"field": "detected_language", "operator": "eq", "value": "ja"}}]
    )
    result = await _compile(reply, text="japanese")
    assert isinstance(result, CompiledCondition)
    assert result.condition == {"field": "detected_language", "operator": "eq", "value": "ja"}


@pytest.mark.asyncio
async def test_prompt_carries_source_text_and_dimensions() -> None:
    llm = _StubLlm("{}")
    await compile_source_text("복잡한 작업", callers=CALLERS, llm=llm)
    system, user = llm.calls[0]
    assert "복잡한 작업" in user
    assert "workflow.agent_loop.plan" in user
    assert "classified_intent" in system
    assert "estimated_tokens" in system
    assert "detected_language" in system
