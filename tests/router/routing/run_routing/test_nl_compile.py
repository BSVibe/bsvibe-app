"""NL → run-routing rules compiler v2 (multi-dimension, NL-native routing Lift N3).

The LLM is a stub — every field the model emits is validated against the caller
registry / the engine's ALLOWED_FIELDS + VALID_OPERATORS / the workspace's
accounts, and the compiler degrades to ``[]`` (never raises) on failure /
unparseable / nothing-valid.

A clause can be about different DIMENSIONS (founder constraint — routing is not
hardcoded to categories): a domain/category (``classified_intent`` + an intent
definition), complexity (``estimated_tokens`` / ``pipeline``), language
(``detected_language``), artifact (``artifact_type_hint``), execution stage
(``caller_id``), or the catch-all default.
"""

from __future__ import annotations

import json

import pytest

from backend.router.routing.run_routing.nl_compile import (
    CompiledProposal,
    CompileLlmUnavailable,
    as_dicts,
    compile_rules,
)

CALLERS = [
    ("workflow.agent_loop.plan", "design step"),
    ("workflow.agent_loop.act", "implementation step"),
    ("workflow.judge", "verifier"),
]
TARGETS = [
    ("dogfood (opus)", "opus"),
    ("dogfood (sonnet)", "sonnet"),
    ("dogfood (haiku)", "haiku"),
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


async def _compile(reply: str, text: str = "마케팅은 sonnet, 나머지는 haiku"):
    return await compile_rules(text, callers=CALLERS, targets=TARGETS, llm=_StubLlm(reply))


# ---------------------------------------------------------------------------
# Caller dimension (back-compat shape from Lift 5)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_caller_dimension_compiles() -> None:
    reply = json.dumps(
        [
            {
                "name": "design → opus",
                "caller_id": "workflow.agent_loop.plan",
                "target": "opus",
                "priority": 10,
                "is_default": False,
            },
            {
                "name": "default → haiku",
                "target": "haiku",
                "is_default": True,
            },
        ]
    )
    rules = await _compile(reply)
    assert as_dicts(rules) == [
        {
            "name": "design → opus",
            "caller_id": "workflow.agent_loop.plan",
            "target": "opus",
            "priority": 10,
            "is_default": False,
            "condition": None,
            "intent_name": None,
            "intent_examples": None,
        },
        {
            "name": "default → haiku",
            "caller_id": None,
            "target": "haiku",
            "priority": 10,
            "is_default": True,
            "condition": None,
            "intent_name": None,
            "intent_examples": None,
        },
    ]


# ---------------------------------------------------------------------------
# Complexity dimension → estimated_tokens
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_complexity_dimension_compiles_to_estimated_tokens() -> None:
    reply = json.dumps(
        [
            {
                "name": "big work → opus",
                "target": "opus",
                "condition": {"field": "estimated_tokens", "operator": "gt", "value": 2000},
                "is_default": False,
            }
        ]
    )
    rules = await _compile(reply, text="복잡한 건 opus")
    assert len(rules) == 1
    p = rules[0]
    assert p.caller_id is None
    assert p.condition == {"field": "estimated_tokens", "operator": "gt", "value": 2000}
    assert p.target == "opus"


@pytest.mark.asyncio
async def test_complexity_dimension_compiles_to_pipeline() -> None:
    reply = json.dumps(
        [
            {
                "name": "design_then_impl → opus",
                "target": "opus",
                "condition": {
                    "field": "pipeline",
                    "operator": "eq",
                    "value": "design_then_impl",
                },
                "is_default": False,
            }
        ]
    )
    rules = await _compile(reply, text="큰 작업은 opus")
    assert rules[0].condition == {
        "field": "pipeline",
        "operator": "eq",
        "value": "design_then_impl",
    }


# ---------------------------------------------------------------------------
# Language dimension → detected_language
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_language_dimension_compiles() -> None:
    reply = json.dumps(
        [
            {
                "name": "korean → sonnet",
                "target": "sonnet",
                "condition": {"field": "detected_language", "operator": "eq", "value": "ko"},
                "is_default": False,
            }
        ]
    )
    rules = await _compile(reply, text="한국어 요청은 sonnet")
    assert rules[0].condition == {
        "field": "detected_language",
        "operator": "eq",
        "value": "ko",
    }


# ---------------------------------------------------------------------------
# Artifact dimension → artifact_type_hint
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_artifact_dimension_compiles() -> None:
    reply = json.dumps(
        [
            {
                "name": "code → opus",
                "target": "opus",
                "condition": {"field": "artifact_type_hint", "operator": "eq", "value": "code"},
                "is_default": False,
            }
        ]
    )
    rules = await _compile(reply, text="코드 작업은 opus")
    assert rules[0].condition == {
        "field": "artifact_type_hint",
        "operator": "eq",
        "value": "code",
    }


# ---------------------------------------------------------------------------
# Category dimension → classified_intent + intent definition
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_category_dimension_emits_intent_and_condition() -> None:
    reply = json.dumps(
        [
            {
                "name": "marketing → sonnet",
                "target": "sonnet",
                "intent_name": "marketing",
                "intent_examples": [
                    "write a marketing email",
                    "draft a landing page headline",
                    "plan a launch campaign",
                ],
                "condition": {
                    "field": "classified_intent",
                    "operator": "eq",
                    "value": "marketing",
                },
                "is_default": False,
            }
        ]
    )
    rules = await _compile(reply, text="마케팅은 sonnet")
    p = rules[0]
    assert p.intent_name == "marketing"
    assert p.intent_examples == [
        "write a marketing email",
        "draft a landing page headline",
        "plan a launch campaign",
    ]
    assert p.condition == {"field": "classified_intent", "operator": "eq", "value": "marketing"}


@pytest.mark.asyncio
async def test_category_condition_value_backfilled_from_intent_name() -> None:
    """When the model names an intent but forgets to key the condition on it (or
    keys it wrong), the compiler forces ``classified_intent == intent_name`` so
    the classifier and the rule agree on the label."""
    reply = json.dumps(
        [
            {
                "name": "design work → opus",
                "target": "opus",
                "intent_name": "design_work",
                "intent_examples": ["design a logo", "create a UI mockup", "pick a color palette"],
                "is_default": False,
            }
        ]
    )
    rules = await _compile(reply, text="디자인 작업은 opus")
    p = rules[0]
    assert p.intent_name == "design_work"
    assert p.condition == {
        "field": "classified_intent",
        "operator": "eq",
        "value": "design_work",
    }


@pytest.mark.asyncio
async def test_category_without_examples_is_dropped() -> None:
    """A category proposal with no seed examples can never classify — drop it."""
    reply = json.dumps(
        [
            {
                "name": "empty category",
                "target": "sonnet",
                "intent_name": "marketing",
                "intent_examples": [],
                "condition": {
                    "field": "classified_intent",
                    "operator": "eq",
                    "value": "marketing",
                },
                "is_default": False,
            }
        ]
    )
    assert await _compile(reply) == []


# ---------------------------------------------------------------------------
# Validation — drop invalid proposals
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drops_hallucinated_target() -> None:
    reply = json.dumps(
        [
            {
                "name": "bad target",
                "target": "gpt-9",
                "condition": {"field": "detected_language", "operator": "eq", "value": "ko"},
                "is_default": False,
            }
        ]
    )
    assert await _compile(reply) == []


@pytest.mark.asyncio
async def test_drops_unknown_condition_field() -> None:
    reply = json.dumps(
        [
            {
                "name": "bad field",
                "target": "sonnet",
                "condition": {"field": "made_up", "operator": "eq", "value": "x"},
                "is_default": False,
            }
        ]
    )
    assert await _compile(reply) == []


@pytest.mark.asyncio
async def test_drops_unknown_condition_operator() -> None:
    reply = json.dumps(
        [
            {
                "name": "bad op",
                "target": "sonnet",
                "condition": {"field": "estimated_tokens", "operator": "approx", "value": 1000},
                "is_default": False,
            }
        ]
    )
    assert await _compile(reply) == []


@pytest.mark.asyncio
async def test_drops_unknown_caller() -> None:
    reply = json.dumps(
        [{"name": "bad caller", "caller_id": "made.up", "target": "opus", "is_default": False}]
    )
    assert await _compile(reply) == []


@pytest.mark.asyncio
async def test_non_default_without_caller_or_condition_is_dropped() -> None:
    reply = json.dumps([{"name": "x", "target": "opus", "is_default": False}])
    assert await _compile(reply) == []


@pytest.mark.asyncio
async def test_default_forces_null_caller_and_only_one_default() -> None:
    reply = json.dumps(
        [
            {"name": "d1", "target": "sonnet", "is_default": True},
            {"name": "d2", "target": "opus", "is_default": True},
        ]
    )
    rules = await _compile(reply)
    assert len(rules) == 1
    assert rules[0].name == "d1"
    assert rules[0].caller_id is None
    assert rules[0].condition is None


@pytest.mark.asyncio
async def test_priority_coerced_and_floored() -> None:
    reply = json.dumps(
        [
            {
                "name": "x",
                "target": "opus",
                "condition": {"field": "detected_language", "operator": "eq", "value": "en"},
                "priority": 0,
            }
        ]
    )
    rules = await _compile(reply)
    assert rules[0].priority == 10  # < 1 → default 10


# ---------------------------------------------------------------------------
# Degradation / prompt
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prompt_carries_dimension_vocabulary() -> None:
    llm = _StubLlm("[]")
    await compile_rules("route it", callers=CALLERS, targets=TARGETS, llm=llm)
    _system, user = llm.calls[0]
    assert "workflow.agent_loop.plan" in user
    assert "opus" in user and "sonnet" in user
    # The system prompt teaches the dimension vocabulary.
    assert "classified_intent" in _system
    assert "estimated_tokens" in _system
    assert "detected_language" in _system


@pytest.mark.asyncio
async def test_tolerates_code_fence() -> None:
    reply = (
        "```json\n["
        '{"name":"x","target":"opus","condition":'
        '{"field":"detected_language","operator":"eq","value":"en"},"is_default":false}'
        "]\n```"
    )
    rules = await _compile(reply)
    assert rules[0].target == "opus"


@pytest.mark.asyncio
async def test_empty_text_returns_empty() -> None:
    assert await compile_rules("  ", callers=CALLERS, targets=TARGETS, llm=_StubLlm("[]")) == []


@pytest.mark.asyncio
async def test_no_targets_returns_empty() -> None:
    assert await compile_rules("route", callers=CALLERS, targets=[], llm=_StubLlm("[]")) == []


@pytest.mark.asyncio
async def test_llm_dispatch_failure_raises_compile_llm_unavailable() -> None:
    """Infrastructure failure is NOT "the founder phrased it badly".

    A raise out of ``llm.complete_text`` (ExecutorAdapterUnavailable, a timeout,
    a provider 5xx) means we never got an ANSWER — degrading to ``[]`` here is
    what let the unwired-redis bug masquerade as "couldn't derive rules". It now
    propagates as :class:`CompileLlmUnavailable` so the endpoint 502s."""
    with pytest.raises(CompileLlmUnavailable):
        await compile_rules("route", callers=CALLERS, targets=TARGETS, llm=_RaisingLlm())


@pytest.mark.asyncio
async def test_unparseable_output_degrades_to_empty() -> None:
    """The model ANSWERED — it just answered with nothing usable. That IS a
    "couldn't derive rules" (422), so the ``[]`` degrade is kept."""
    assert await _compile("sorry, I can't help with that") == []


@pytest.mark.asyncio
async def test_as_dicts_matches_wire_shape() -> None:
    p = CompiledProposal(
        name="marketing → sonnet",
        caller_id=None,
        target="sonnet",
        priority=10,
        is_default=False,
        condition={"field": "classified_intent", "operator": "eq", "value": "marketing"},
        intent_name="marketing",
        intent_examples=["write a marketing email"],
    )
    assert as_dicts([p]) == [
        {
            "name": "marketing → sonnet",
            "caller_id": None,
            "target": "sonnet",
            "priority": 10,
            "is_default": False,
            "condition": {"field": "classified_intent", "operator": "eq", "value": "marketing"},
            "intent_name": "marketing",
            "intent_examples": ["write a marketing email"],
        }
    ]


@pytest.mark.asyncio
async def test_compile_for_workspace_uses_active_accounts_as_targets() -> None:
    """The shared helper (used by REST + MCP) builds the model catalog from the
    workspace's ACTIVE accounts and returns wire-shaped proposals."""
    import uuid

    from backend.api.v1.run_routing import compile_for_workspace
    from backend.router.accounts.models import ModelAccount

    from ...._support import memory_session

    def _acct(ws: uuid.UUID, litellm_model: str) -> ModelAccount:
        return ModelAccount(
            id=uuid.uuid4(),
            workspace_id=ws,
            account_id=uuid.uuid4(),
            provider="executor",
            label=f"dogfood ({litellm_model})",
            litellm_model=litellm_model,
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="unknown",
            is_active=True,
            extra_params={"executor_type": "claude_code", "worker_id": str(uuid.uuid4())},
        )

    reply = json.dumps(
        [
            {
                "name": "marketing → sonnet",
                "target": "sonnet",
                "intent_name": "marketing",
                "intent_examples": ["write a marketing email", "plan a campaign", "draft copy"],
                "condition": {
                    "field": "classified_intent",
                    "operator": "eq",
                    "value": "marketing",
                },
                "is_default": False,
            },
            {"name": "default → haiku", "target": "haiku", "is_default": True},
        ]
    )
    ws = uuid.uuid4()
    async with memory_session() as s:
        s.add_all([_acct(ws, "sonnet"), _acct(ws, "haiku")])
        await s.commit()
        proposals = await compile_for_workspace(s, ws, "마케팅은 sonnet", llm=_StubLlm(reply))

    assert proposals[0]["intent_name"] == "marketing"
    assert proposals[0]["condition"] == {
        "field": "classified_intent",
        "operator": "eq",
        "value": "marketing",
    }
    assert proposals[1]["is_default"] is True
    assert proposals[1]["target"] == "haiku"
