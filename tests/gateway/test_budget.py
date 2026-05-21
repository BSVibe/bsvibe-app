"""Tests for backend.gateway.budget — tracker + policy enforcement."""

from __future__ import annotations

import uuid

import pytest

from backend.gateway.budget.errors import BudgetExceeded
from backend.gateway.budget.models import BudgetEnforcement, BudgetScope
from backend.gateway.budget.policy import BudgetPolicyService
from backend.gateway.budget.repository import BudgetPolicyRepository
from backend.gateway.budget.tracker import BudgetTracker, InMemoryBudgetStore


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def tracker() -> BudgetTracker:
    return BudgetTracker(InMemoryBudgetStore())


@pytest.fixture
def policy_service(session, tracker):
    repo = BudgetPolicyRepository(session)
    return BudgetPolicyService(repository=repo, tracker=tracker), repo


class TestTracker:
    async def test_record_increments_daily_and_monthly(self, tracker, workspace_id, account_id):
        await tracker.record_cost(workspace_id=workspace_id, account_id=account_id, cost_cents=120)
        assert (await tracker.daily_cost(workspace_id=workspace_id, account_id=account_id)) == 120
        assert (await tracker.monthly_cost(workspace_id=workspace_id, account_id=account_id)) == 120

    async def test_isolated_per_account(self, tracker, workspace_id, account_id):
        other_account = uuid.uuid4()
        await tracker.record_cost(workspace_id=workspace_id, account_id=account_id, cost_cents=50)
        assert (await tracker.daily_cost(workspace_id=workspace_id, account_id=other_account)) == 0

    async def test_keys_use_account_namespace(self, tracker, workspace_id, account_id):
        key = BudgetTracker._key(  # noqa: SLF001
            workspace_id=workspace_id,
            account_id=account_id,
            scope="daily",
            period="2026-05-21",
        )
        assert f"ws:{workspace_id}:acct:{account_id}:cost:daily:2026-05-21" == key


class TestRepository:
    async def test_upsert_inserts_and_updates(self, session, workspace_id, account_id):
        repo = BudgetPolicyRepository(session)
        row = await repo.upsert(
            workspace_id=workspace_id,
            account_id=account_id,
            scope=BudgetScope.DAILY,
            cost_cap_cents=1_000,
        )
        assert row.cost_cap_cents == 1_000

        row2 = await repo.upsert(
            workspace_id=workspace_id,
            account_id=account_id,
            scope=BudgetScope.DAILY,
            cost_cap_cents=2_000,
            enforcement=BudgetEnforcement.WARN,
        )
        assert row2.id == row.id
        assert row2.cost_cap_cents == 2_000
        assert row2.enforcement is BudgetEnforcement.WARN

    async def test_delete_returns_true_then_false(self, session, workspace_id, account_id):
        repo = BudgetPolicyRepository(session)
        await repo.upsert(
            workspace_id=workspace_id,
            account_id=account_id,
            scope=BudgetScope.DAILY,
            cost_cap_cents=1_000,
        )
        assert (
            await repo.delete(
                workspace_id=workspace_id,
                account_id=account_id,
                scope=BudgetScope.DAILY,
            )
            is True
        )
        assert (
            await repo.delete(
                workspace_id=workspace_id,
                account_id=account_id,
                scope=BudgetScope.DAILY,
            )
            is False
        )

    async def test_list_returns_all_scopes_for_account(self, session, workspace_id, account_id):
        repo = BudgetPolicyRepository(session)
        await repo.upsert(
            workspace_id=workspace_id,
            account_id=account_id,
            scope=BudgetScope.DAILY,
            cost_cap_cents=1_000,
        )
        await repo.upsert(
            workspace_id=workspace_id,
            account_id=account_id,
            scope=BudgetScope.MONTHLY,
            cost_cap_cents=10_000,
        )
        rows = await repo.list_(workspace_id=workspace_id, account_id=account_id)
        assert len(rows) == 2


class TestPolicyService:
    async def test_no_policy_means_no_block(self, policy_service, workspace_id, account_id):
        svc, _ = policy_service
        result = await svc.check_request_cost(
            workspace_id=workspace_id,
            account_id=account_id,
            projected_cost_cents=100,
        )
        assert result.blocked is False
        assert result.breached_scopes == ()

    async def test_below_cap_allowed(self, policy_service, workspace_id, account_id):
        svc, repo = policy_service
        await repo.upsert(
            workspace_id=workspace_id,
            account_id=account_id,
            scope=BudgetScope.DAILY,
            cost_cap_cents=1_000,
        )
        await svc.record_actual_cost(
            workspace_id=workspace_id, account_id=account_id, cost_cents=400
        )
        result = await svc.check_request_cost(
            workspace_id=workspace_id,
            account_id=account_id,
            projected_cost_cents=200,
        )
        assert result.blocked is False
        assert result.breached_scopes == ()

    async def test_exceed_cap_block_raises(self, policy_service, workspace_id, account_id):
        svc, repo = policy_service
        await repo.upsert(
            workspace_id=workspace_id,
            account_id=account_id,
            scope=BudgetScope.DAILY,
            cost_cap_cents=500,
            enforcement=BudgetEnforcement.BLOCK,
        )
        await svc.record_actual_cost(
            workspace_id=workspace_id, account_id=account_id, cost_cents=400
        )
        with pytest.raises(BudgetExceeded) as exc_info:
            await svc.check_request_cost(
                workspace_id=workspace_id,
                account_id=account_id,
                projected_cost_cents=200,
            )
        assert exc_info.value.scope == "daily"
        assert exc_info.value.cap_cents == 500

    async def test_warn_mode_does_not_raise_but_reports_breach(
        self, policy_service, workspace_id, account_id
    ):
        svc, repo = policy_service
        await repo.upsert(
            workspace_id=workspace_id,
            account_id=account_id,
            scope=BudgetScope.DAILY,
            cost_cap_cents=500,
            enforcement=BudgetEnforcement.WARN,
        )
        await svc.record_actual_cost(
            workspace_id=workspace_id, account_id=account_id, cost_cents=400
        )
        result = await svc.check_request_cost(
            workspace_id=workspace_id,
            account_id=account_id,
            projected_cost_cents=200,
        )
        assert result.blocked is False
        assert "daily" in result.breached_scopes

    async def test_monthly_cap_evaluated_separately(self, policy_service, workspace_id, account_id):
        svc, repo = policy_service
        await repo.upsert(
            workspace_id=workspace_id,
            account_id=account_id,
            scope=BudgetScope.MONTHLY,
            cost_cap_cents=1_000,
            enforcement=BudgetEnforcement.BLOCK,
        )
        await svc.record_actual_cost(
            workspace_id=workspace_id, account_id=account_id, cost_cents=900
        )
        with pytest.raises(BudgetExceeded) as exc_info:
            await svc.check_request_cost(
                workspace_id=workspace_id,
                account_id=account_id,
                projected_cost_cents=200,
            )
        assert exc_info.value.scope == "monthly"
