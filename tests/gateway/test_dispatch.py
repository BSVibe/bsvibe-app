"""Tests for backend.gateway.dispatch — end-to-end orchestration."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from backend.gateway.accounts.crypto import CredentialCipher
from backend.gateway.accounts.schemas import ModelAccountCreate
from backend.gateway.accounts.service import ModelAccountService
from backend.gateway.budget.errors import BudgetExceeded
from backend.gateway.budget.models import BudgetEnforcement, BudgetScope
from backend.gateway.budget.policy import BudgetPolicyService
from backend.gateway.budget.repository import BudgetPolicyRepository
from backend.gateway.budget.tracker import BudgetTracker, InMemoryBudgetStore
from backend.gateway.classifier.base import (
    ClassificationFeatures,
    ClassificationResult,
)
from backend.gateway.dispatch import (
    DispatchRequest,
    GatewayDispatcher,
    ModelAccountNotFound,
)
from backend.gateway.llm_client import LlmClient


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = type("M", (), {"content": content})()


class _Response:
    def __init__(self, content: str = "ok") -> None:
        self.choices = [_Choice(content)]
        self.usage = type("U", (), {"prompt_tokens": 100, "completion_tokens": 50})()


class _StaticClassifier:
    def __init__(self, tier: str = "cloud") -> None:
        self.tier = tier

    async def classify(self, features):
        return ClassificationResult(tier=self.tier, score=50, strategy="stub")  # type: ignore[arg-type]


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


def _features() -> ClassificationFeatures:
    return ClassificationFeatures(0, 0, 0, 0, 0)


async def _make_account(service, workspace_id, account_id):
    return await service.create(
        workspace_id=workspace_id,
        account_id=account_id,
        payload=ModelAccountCreate(
            provider="openai",
            label="gpt4o",
            litellm_model="openai/gpt-4o",
            api_key="sk-secret",
            data_jurisdiction="us",
        ),
    )


class TestHappyPath:
    async def test_dispatch_records_cost_and_returns_response(
        self, session, cipher: CredentialCipher, workspace_id, account_id
    ):
        accounts = ModelAccountService(session, cipher=cipher)
        ma = await _make_account(accounts, workspace_id, account_id)

        repo = BudgetPolicyRepository(session)
        tracker = BudgetTracker(InMemoryBudgetStore())
        budget = BudgetPolicyService(repository=repo, tracker=tracker)
        fake_completion = AsyncMock(return_value=_Response("hi"))
        llm = LlmClient(completion_fn=fake_completion)

        dispatcher = GatewayDispatcher(
            accounts=accounts,
            classifier=_StaticClassifier(tier="cloud"),
            budget=budget,
            llm=llm,
            cost_per_token_cents=0.01,
        )

        result = await dispatcher.dispatch(
            DispatchRequest(
                workspace_id=workspace_id,
                account_id=account_id,
                model_account_id=ma.id,
                messages=[{"role": "user", "content": "hi"}],
                features=_features(),
                projected_cost_cents=10,
            )
        )

        assert result.response.content == "hi"
        assert result.classification.tier == "cloud"
        # 150 tokens × 0.01 = 1.5 → rounded to 2.
        assert result.actual_cost_cents == 2

        # Tracker should have the actual cost recorded.
        assert (await tracker.daily_cost(workspace_id=workspace_id, account_id=account_id)) == 2

        # The fake litellm completion must have been called with the
        # decrypted credentials.
        kwargs = fake_completion.await_args.kwargs
        assert kwargs["api_key"] == "sk-secret"
        assert kwargs["model"] == "openai/gpt-4o"


class TestErrors:
    async def test_missing_model_account_raises(self, session, cipher, workspace_id, account_id):
        accounts = ModelAccountService(session, cipher=cipher)
        repo = BudgetPolicyRepository(session)
        tracker = BudgetTracker(InMemoryBudgetStore())
        budget = BudgetPolicyService(repository=repo, tracker=tracker)
        dispatcher = GatewayDispatcher(
            accounts=accounts,
            classifier=_StaticClassifier(),
            budget=budget,
            llm=LlmClient(completion_fn=AsyncMock()),
        )
        with pytest.raises(ModelAccountNotFound):
            await dispatcher.dispatch(
                DispatchRequest(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    model_account_id=uuid.uuid4(),
                    messages=[],
                    features=_features(),
                    projected_cost_cents=10,
                )
            )

    async def test_budget_exceeded_blocks_dispatch(self, session, cipher, workspace_id, account_id):
        accounts = ModelAccountService(session, cipher=cipher)
        ma = await _make_account(accounts, workspace_id, account_id)

        repo = BudgetPolicyRepository(session)
        await repo.upsert(
            workspace_id=workspace_id,
            account_id=account_id,
            scope=BudgetScope.DAILY,
            cost_cap_cents=100,
            enforcement=BudgetEnforcement.BLOCK,
        )
        tracker = BudgetTracker(InMemoryBudgetStore())
        await tracker.record_cost(workspace_id=workspace_id, account_id=account_id, cost_cents=99)
        budget = BudgetPolicyService(repository=repo, tracker=tracker)
        fake_completion = AsyncMock(return_value=_Response("never called"))
        llm = LlmClient(completion_fn=fake_completion)

        dispatcher = GatewayDispatcher(
            accounts=accounts,
            classifier=_StaticClassifier(),
            budget=budget,
            llm=llm,
        )
        with pytest.raises(BudgetExceeded):
            await dispatcher.dispatch(
                DispatchRequest(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    model_account_id=ma.id,
                    messages=[],
                    features=_features(),
                    projected_cost_cents=50,
                )
            )
        # Crucially, the LLM was never dispatched.
        fake_completion.assert_not_awaited()

    async def test_inactive_account_raises_not_found(
        self, session, cipher, workspace_id, account_id
    ):
        accounts = ModelAccountService(session, cipher=cipher)
        ma = await _make_account(accounts, workspace_id, account_id)
        from backend.gateway.accounts.schemas import ModelAccountUpdate

        await accounts.update(
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=ma.id,
            payload=ModelAccountUpdate(is_active=False),
        )
        repo = BudgetPolicyRepository(session)
        budget = BudgetPolicyService(repository=repo, tracker=BudgetTracker(InMemoryBudgetStore()))
        dispatcher = GatewayDispatcher(
            accounts=accounts,
            classifier=_StaticClassifier(),
            budget=budget,
            llm=LlmClient(completion_fn=AsyncMock()),
        )
        with pytest.raises(ModelAccountNotFound):
            await dispatcher.dispatch(
                DispatchRequest(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    model_account_id=ma.id,
                    messages=[],
                    features=_features(),
                    projected_cost_cents=10,
                )
            )
