"""Lift I-Repo-Router — SqlAlchemyRunRoutingRuleRepository round-trip tests."""

from __future__ import annotations

import uuid

import pytest

from backend.router.infrastructure.repositories import SqlAlchemyRunRoutingRuleRepository
from backend.router.routing.run_routing.db import RunRoutingRuleRow
from tests._support import memory_session


def _rule(
    *,
    workspace_id: uuid.UUID,
    name: str,
    target: str = "ollama/qwen3",
    priority: int = 10,
    is_default: bool = False,
) -> RunRoutingRuleRow:
    return RunRoutingRuleRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=name,
        priority=priority,
        is_default=is_default,
        target=target,
        conditions=[],
        is_active=True,
    )


@pytest.mark.asyncio
async def test_add_and_list_by_workspace_priority_asc() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyRunRoutingRuleRepository(session)
        ws = uuid.uuid4()
        await repo.add(_rule(workspace_id=ws, name="high", priority=20))
        await repo.add(_rule(workspace_id=ws, name="low", priority=5))
        await repo.add(_rule(workspace_id=ws, name="mid", priority=10))

        rows = await repo.list_by_workspace(workspace_id=ws)
        assert [r.name for r in rows] == ["low", "mid", "high"]


@pytest.mark.asyncio
async def test_list_by_workspace_scoped() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyRunRoutingRuleRepository(session)
        ws_a = uuid.uuid4()
        ws_b = uuid.uuid4()
        await repo.add(_rule(workspace_id=ws_a, name="a"))
        await repo.add(_rule(workspace_id=ws_b, name="b"))

        rows_a = await repo.list_by_workspace(workspace_id=ws_a)
        assert {r.name for r in rows_a} == {"a"}


@pytest.mark.asyncio
async def test_get_scoped_by_workspace() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyRunRoutingRuleRepository(session)
        ws = uuid.uuid4()
        other = uuid.uuid4()
        rule = _rule(workspace_id=ws, name="r")
        await repo.add(rule)

        got = await repo.get(workspace_id=ws, rule_id=rule.id)
        assert got is not None
        assert got.id == rule.id

        # Cross-workspace must return None — not leak the row's existence.
        assert await repo.get(workspace_id=other, rule_id=rule.id) is None


@pytest.mark.asyncio
async def test_has_any_false_when_empty() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyRunRoutingRuleRepository(session)
        assert await repo.has_any(workspace_id=uuid.uuid4()) is False


@pytest.mark.asyncio
async def test_has_any_true_with_rule() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyRunRoutingRuleRepository(session)
        ws = uuid.uuid4()
        await repo.add(_rule(workspace_id=ws, name="any"))

        assert await repo.has_any(workspace_id=ws) is True
        # Different workspace remains empty.
        assert await repo.has_any(workspace_id=uuid.uuid4()) is False


@pytest.mark.asyncio
async def test_delete_removes_row() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyRunRoutingRuleRepository(session)
        ws = uuid.uuid4()
        rule = _rule(workspace_id=ws, name="to_delete")
        await repo.add(rule)

        await repo.delete(rule)
        assert await repo.get(workspace_id=ws, rule_id=rule.id) is None


@pytest.mark.asyncio
async def test_add_raises_integrity_on_duplicate_name() -> None:
    from sqlalchemy.exc import IntegrityError

    async with memory_session() as session:
        repo = SqlAlchemyRunRoutingRuleRepository(session)
        ws = uuid.uuid4()
        await repo.add(_rule(workspace_id=ws, name="dup"))
        with pytest.raises(IntegrityError):
            await repo.add(_rule(workspace_id=ws, name="dup"))
