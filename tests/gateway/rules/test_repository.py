"""RulesRepository — CRUD round-trip + condition replace + reorder."""

from __future__ import annotations

import uuid

import pytest

from backend.gateway.rules.repository import RulesRepository


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


class TestRulesCRUD:
    async def test_create_and_get(self, session, workspace_id, account_id):
        repo = RulesRepository(session)
        rule = await repo.create_rule(
            workspace_id=workspace_id,
            account_id=account_id,
            name="urgent_to_premium",
            priority=10,
            target_model="claude-3-opus",
        )
        assert rule.id is not None
        assert rule.target_model == "claude-3-opus"

        fetched = await repo.get_rule(rule.id, workspace_id=workspace_id, account_id=account_id)
        assert fetched is not None
        assert fetched.id == rule.id

    async def test_list_rules_sorted_by_priority(self, session, workspace_id, account_id):
        repo = RulesRepository(session)
        await repo.create_rule(
            workspace_id=workspace_id,
            account_id=account_id,
            name="lo",
            priority=50,
            target_model="a",
        )
        await repo.create_rule(
            workspace_id=workspace_id,
            account_id=account_id,
            name="hi",
            priority=1,
            target_model="b",
        )
        rules = await repo.list_rules(workspace_id=workspace_id, account_id=account_id)
        assert [r.name for r in rules] == ["hi", "lo"]

    async def test_unique_name_per_account(self, session, workspace_id, account_id):
        repo = RulesRepository(session)
        await repo.create_rule(
            workspace_id=workspace_id,
            account_id=account_id,
            name="dup",
            priority=10,
            target_model="a",
        )
        with pytest.raises(Exception):  # noqa: B017,PT011 — repository may wrap; the constraint matters
            await repo.create_rule(
                workspace_id=workspace_id,
                account_id=account_id,
                name="dup",
                priority=20,
                target_model="b",
            )

    async def test_update_rule(self, session, workspace_id, account_id):
        repo = RulesRepository(session)
        rule = await repo.create_rule(
            workspace_id=workspace_id,
            account_id=account_id,
            name="orig",
            priority=10,
            target_model="a",
        )
        updated = await repo.update_rule(
            rule.id,
            workspace_id=workspace_id,
            account_id=account_id,
            name="renamed",
            priority=20,
            is_default=False,
            target_model="b",
        )
        assert updated is not None
        assert updated.name == "renamed"
        assert updated.target_model == "b"

    async def test_delete_rule(self, session, workspace_id, account_id):
        repo = RulesRepository(session)
        rule = await repo.create_rule(
            workspace_id=workspace_id,
            account_id=account_id,
            name="doomed",
            priority=10,
            target_model="a",
        )
        deleted = await repo.delete_rule(rule.id, workspace_id=workspace_id, account_id=account_id)
        assert deleted is True
        again = await repo.get_rule(rule.id, workspace_id=workspace_id, account_id=account_id)
        assert again is None


class TestConditions:
    async def test_replace_conditions(self, session, workspace_id, account_id):
        repo = RulesRepository(session)
        rule = await repo.create_rule(
            workspace_id=workspace_id,
            account_id=account_id,
            name="r",
            priority=10,
            target_model="x",
        )
        await repo.replace_conditions(
            rule.id,
            [
                {
                    "condition_type": "text_pattern",
                    "operator": "contains",
                    "field": "user_text",
                    "value": "urgent",
                },
                {
                    "condition_type": "token_count",
                    "operator": "gt",
                    "field": "estimated_tokens",
                    "value": 100,
                },
            ],
        )
        conds = await repo.list_conditions(rule.id)
        assert len(conds) == 2
        # replace again with empty list → all gone
        await repo.replace_conditions(rule.id, [])
        conds = await repo.list_conditions(rule.id)
        assert conds == []


class TestReorder:
    async def test_reorder_swaps_priorities(self, session, workspace_id, account_id):
        repo = RulesRepository(session)
        a = await repo.create_rule(
            workspace_id=workspace_id,
            account_id=account_id,
            name="a",
            priority=1,
            target_model="x",
        )
        b = await repo.create_rule(
            workspace_id=workspace_id,
            account_id=account_id,
            name="b",
            priority=2,
            target_model="y",
        )
        await repo.reorder_rules(
            workspace_id=workspace_id,
            account_id=account_id,
            priorities={a.id: 2, b.id: 1},
        )
        rules = await repo.list_rules(workspace_id=workspace_id, account_id=account_id)
        # After reorder, b (priority 1) lists first.
        assert [r.name for r in rules] == ["b", "a"]
