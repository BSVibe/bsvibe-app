"""NL → run-routing rules compiler (unified routing Lift 5).

The LLM is a stub — every field the model emits is validated against the caller
registry / the workspace's accounts, and the compiler degrades to ``[]`` (never
raises) on failure / unparseable / nothing-valid.
"""

from __future__ import annotations

import json

import pytest

from backend.router.routing.run_routing.nl_compile import (
    CompiledRule,
    as_dicts,
    compile_rules,
)

CALLERS = [
    ("workflow.agent_loop.plan", "design step"),
    ("workflow.agent_loop.act", "implementation step"),
    ("workflow.judge", "verifier"),
]
TARGETS = [("dogfood (opus)", "opus"), ("dogfood (sonnet)", "sonnet")]


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


async def _compile(reply: str, text: str = "설계는 opus, 나머지는 sonnet"):
    return await compile_rules(text, callers=CALLERS, targets=TARGETS, llm=_StubLlm(reply))


@pytest.mark.asyncio
async def test_compiles_design_to_opus_default_to_sonnet() -> None:
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
                "name": "default → sonnet",
                "caller_id": None,
                "target": "sonnet",
                "priority": 100,
                "is_default": True,
            },
        ]
    )
    rules = await _compile(reply)
    assert rules == [
        CompiledRule("design → opus", "workflow.agent_loop.plan", "opus", 10, False),
        CompiledRule("default → sonnet", None, "sonnet", 100, True),
    ]


@pytest.mark.asyncio
async def test_prompt_carries_caller_and_model_catalogs() -> None:
    llm = _StubLlm("[]")
    await compile_rules("route it", callers=CALLERS, targets=TARGETS, llm=llm)
    _system, user = llm.calls[0]
    assert "workflow.agent_loop.plan" in user
    assert "opus" in user and "sonnet" in user


@pytest.mark.asyncio
async def test_drops_hallucinated_caller_and_target() -> None:
    reply = json.dumps(
        [
            {"name": "bad caller", "caller_id": "made.up", "target": "opus", "is_default": False},
            {
                "name": "bad target",
                "caller_id": "workflow.judge",
                "target": "gpt-9",
                "is_default": False,
            },
            {"name": "ok", "caller_id": "workflow.judge", "target": "sonnet", "is_default": False},
        ]
    )
    rules = await _compile(reply)
    assert [r.name for r in rules] == ["ok"]


@pytest.mark.asyncio
async def test_default_rule_forces_null_caller_and_only_one_default() -> None:
    reply = json.dumps(
        [
            {"name": "d1", "caller_id": "workflow.judge", "target": "sonnet", "is_default": True},
            {"name": "d2", "caller_id": None, "target": "opus", "is_default": True},
        ]
    )
    rules = await _compile(reply)
    assert len(rules) == 1
    assert rules[0].name == "d1"
    assert rules[0].caller_id is None  # is_default forces caller_id null


@pytest.mark.asyncio
async def test_non_default_without_known_caller_is_dropped() -> None:
    reply = json.dumps([{"name": "x", "caller_id": None, "target": "opus", "is_default": False}])
    assert await _compile(reply) == []


@pytest.mark.asyncio
async def test_priority_coerced_and_floored() -> None:
    reply = json.dumps(
        [{"name": "x", "caller_id": "workflow.judge", "target": "opus", "priority": 0}]
    )
    rules = await _compile(reply)
    assert rules[0].priority == 10  # < 1 → default 10


@pytest.mark.asyncio
async def test_tolerates_code_fence() -> None:
    reply = '```json\n[{"name":"x","caller_id":"workflow.judge","target":"opus","is_default":false}]\n```'
    rules = await _compile(reply)
    assert rules[0].target == "opus"


@pytest.mark.asyncio
async def test_empty_text_returns_empty() -> None:
    assert await compile_rules("  ", callers=CALLERS, targets=TARGETS, llm=_StubLlm("[]")) == []


@pytest.mark.asyncio
async def test_no_targets_returns_empty() -> None:
    assert await compile_rules("route", callers=CALLERS, targets=[], llm=_StubLlm("[]")) == []


@pytest.mark.asyncio
async def test_llm_failure_degrades_to_empty() -> None:
    assert await compile_rules("route", callers=CALLERS, targets=TARGETS, llm=_RaisingLlm()) == []


@pytest.mark.asyncio
async def test_unparseable_output_degrades_to_empty() -> None:
    assert await _compile("sorry, I can't help with that") == []


@pytest.mark.asyncio
async def test_compile_for_workspace_uses_active_accounts_as_targets() -> None:
    """The shared helper (used by REST + MCP) builds the model catalog from the
    workspace's ACTIVE accounts and returns create-shaped proposals."""
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
                "name": "design → opus",
                "caller_id": "workflow.agent_loop.plan",
                "target": "opus",
                "priority": 10,
                "is_default": False,
            },
            {"name": "default → sonnet", "caller_id": None, "target": "sonnet", "is_default": True},
        ]
    )
    ws = uuid.uuid4()
    async with memory_session() as s:
        s.add_all([_acct(ws, "opus"), _acct(ws, "sonnet")])
        await s.commit()
        proposals = await compile_for_workspace(s, ws, "설계는 opus", llm=_StubLlm(reply))

    assert proposals == [
        {
            "name": "design → opus",
            "caller_id": "workflow.agent_loop.plan",
            "target": "opus",
            "priority": 10,
            "is_default": False,
        },
        {
            "name": "default → sonnet",
            "caller_id": None,
            "target": "sonnet",
            "priority": 10,
            "is_default": True,
        },
    ]


@pytest.mark.asyncio
async def test_as_dicts_matches_create_shape() -> None:
    rules = [CompiledRule("design → opus", "workflow.agent_loop.plan", "opus", 10, False)]
    assert as_dicts(rules) == [
        {
            "name": "design → opus",
            "caller_id": "workflow.agent_loop.plan",
            "target": "opus",
            "priority": 10,
            "is_default": False,
        }
    ]
