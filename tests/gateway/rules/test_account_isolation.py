"""Two accounts in the same workspace see disjoint rule sets."""

from __future__ import annotations

import uuid

import pytest

from backend.gateway.rules.repository import RulesRepository


class TestAccountIsolation:
    async def test_list_rules_filters_by_account(self, session):
        ws = uuid.uuid4()
        acct_a = uuid.uuid4()
        acct_b = uuid.uuid4()
        repo = RulesRepository(session)

        await repo.create_rule(
            workspace_id=ws,
            account_id=acct_a,
            name="a_rule",
            priority=1,
            target_model="model-a",
        )
        await repo.create_rule(
            workspace_id=ws,
            account_id=acct_b,
            name="b_rule",
            priority=1,
            target_model="model-b",
        )

        a_rules = await repo.list_rules(workspace_id=ws, account_id=acct_a)
        b_rules = await repo.list_rules(workspace_id=ws, account_id=acct_b)
        assert [r.name for r in a_rules] == ["a_rule"]
        assert [r.name for r in b_rules] == ["b_rule"]

    async def test_get_rule_account_scoped(self, session):
        ws = uuid.uuid4()
        acct_a = uuid.uuid4()
        acct_b = uuid.uuid4()
        repo = RulesRepository(session)
        rule = await repo.create_rule(
            workspace_id=ws,
            account_id=acct_a,
            name="r",
            priority=1,
            target_model="x",
        )
        # Same workspace, different account → should not see it.
        assert await repo.get_rule(rule.id, workspace_id=ws, account_id=acct_b) is None
        assert await repo.get_rule(rule.id, workspace_id=ws, account_id=acct_a) is not None

    async def test_same_priority_allowed_across_accounts(self, session):
        """Priority 1 in account A doesn't collide with priority 1 in account B."""
        ws = uuid.uuid4()
        acct_a = uuid.uuid4()
        acct_b = uuid.uuid4()
        repo = RulesRepository(session)
        await repo.create_rule(
            workspace_id=ws,
            account_id=acct_a,
            name="r1",
            priority=1,
            target_model="x",
        )
        # No exception expected.
        await repo.create_rule(
            workspace_id=ws,
            account_id=acct_b,
            name="r1",
            priority=1,
            target_model="x",
        )

    async def test_same_name_within_account_rejected(self, session):
        ws = uuid.uuid4()
        acct = uuid.uuid4()
        repo = RulesRepository(session)
        await repo.create_rule(
            workspace_id=ws,
            account_id=acct,
            name="dup",
            priority=1,
            target_model="x",
        )
        with pytest.raises(Exception):  # noqa: B017,PT011
            await repo.create_rule(
                workspace_id=ws,
                account_id=acct,
                name="dup",
                priority=2,
                target_model="y",
            )
