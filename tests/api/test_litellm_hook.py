"""LiteLLM hook + ChatService — wired against RuleEngine + budget service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from backend.api.litellm_hook import ChatService, LiteLLMHook
from backend.api.litellm_hook.chat_service import ChatCompletionContext
from backend.api.litellm_hook.hook import HookContext, HookDependencies
from backend.router.budget.errors import BudgetExceeded
from backend.router.budget.policy import BudgetCheckResult
from backend.router.rules.engine import RuleEngine
from backend.router.rules.models import RoutingRule, RuleMatch


def _ctx(account_id: uuid.UUID | None = None) -> HookContext:
    return HookContext(
        workspace_id=uuid.uuid4(),
        account_id=account_id,
        user_id=None,
        trace_id="t",
    )


@dataclass
class _FakeBudget:
    block: bool = False
    breached: tuple[str, ...] = ()

    async def check_request_cost(self, **_kwargs):
        return BudgetCheckResult(
            blocked=self.block,
            breached_scopes=self.breached,
            daily_current_cents=0,
            monthly_current_cents=0,
        )


@pytest.mark.asyncio
async def test_hook_passthrough_when_no_account() -> None:
    hook = LiteLLMHook(
        context=_ctx(account_id=None),
        deps=HookDependencies(rule_engine=RuleEngine(), rules=[]),
    )
    payload = {"model": "x", "messages": []}
    out = await hook.async_pre_call_hook(payload)
    assert out is payload


@pytest.mark.asyncio
async def test_hook_rule_match_rewrites_model() -> None:
    rule = RoutingRule(
        id=uuid.uuid4(),
        name="cheap-haiku",
        priority=1,
        workspace_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        conditions=[],
        target_model="anthropic/claude-haiku-4-5",
        is_default=True,
        is_active=True,
    )

    # We monkey-patch the rule engine to return a synthetic match without
    # exercising the condition machinery (a separate test concern).
    class _StubEngine:
        async def evaluate(self, _data, **_kwargs):
            return RuleMatch(rule=rule, target_model="anthropic/claude-haiku-4-5", trace=["match"])

    hook = LiteLLMHook(
        context=_ctx(account_id=uuid.uuid4()),
        deps=HookDependencies(rule_engine=_StubEngine(), rules=[rule]),  # type: ignore[arg-type]
    )
    data = {"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    out = await hook.async_pre_call_hook(data)
    assert out["model"] == "anthropic/claude-haiku-4-5"


@pytest.mark.asyncio
async def test_hook_budget_exceeded_raises() -> None:
    hook = LiteLLMHook(
        context=_ctx(account_id=uuid.uuid4()),
        deps=HookDependencies(
            rule_engine=RuleEngine(),
            rules=[],
            budget_service=_FakeBudget(block=True, breached=("daily",)),  # type: ignore[arg-type]
            estimated_cost_cents=42,
        ),
    )
    with pytest.raises(BudgetExceeded, match="daily"):
        await hook.async_pre_call_hook({"model": "x", "messages": []})


@pytest.mark.asyncio
async def test_hook_budget_pass_does_not_raise() -> None:
    hook = LiteLLMHook(
        context=_ctx(account_id=uuid.uuid4()),
        deps=HookDependencies(
            rule_engine=RuleEngine(),
            rules=[],
            budget_service=_FakeBudget(block=False),  # type: ignore[arg-type]
            estimated_cost_cents=0,
        ),
    )
    out = await hook.async_pre_call_hook({"model": "x", "messages": []})
    assert out == {"model": "x", "messages": []}


@pytest.mark.asyncio
async def test_chat_service_complete_requires_dispatcher() -> None:
    svc = ChatService()
    cctx = ChatCompletionContext(
        workspace_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        trace_id="t",
        stream=False,
        model_account_id=uuid.uuid4(),
    )
    with pytest.raises(RuntimeError, match="requires a GatewayDispatcher"):
        await svc.complete(context=cctx, payload={"messages": []})


@pytest.mark.asyncio
async def test_chat_service_complete_rejects_missing_account() -> None:
    """Without account_id, complete() refuses — every dispatch needs scoping."""

    class _NeverDispatcher:
        async def dispatch(self, _req):  # pragma: no cover — should never be called
            raise AssertionError("dispatcher should not run")

    svc = ChatService(dispatcher=_NeverDispatcher())  # type: ignore[arg-type]
    cctx = ChatCompletionContext(
        workspace_id=uuid.uuid4(),
        account_id=None,
        trace_id="t",
        stream=False,
        model_account_id=None,
    )
    with pytest.raises(ValueError, match="account_id"):
        await svc.complete(context=cctx, payload={"messages": []})
