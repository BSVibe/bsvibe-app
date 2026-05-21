"""RoutingLogsRepository — insert + per-account usage aggregates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.gateway.routing.logs_repository import (
    RoutingLogFeatures,
    RoutingLogsRepository,
)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


def _features(**overrides) -> RoutingLogFeatures:
    base = dict(
        token_count=100,
        conversation_turns=1,
        code_block_count=0,
        code_lines=0,
        has_error_trace=False,
        tool_count=0,
    )
    base.update(overrides)
    return RoutingLogFeatures(**base)


class TestInsert:
    async def test_round_trip(self, session, workspace_id, account_id):
        repo = RoutingLogsRepository(session)
        await repo.insert_routing_log(
            workspace_id=workspace_id,
            account_id=account_id,
            rule_id=None,
            user_text="hi",
            system_prompt="",
            features=_features(),
            tier="local",
            strategy="static",
            score=10,
            original_model="gpt-4o",
            resolved_model="ollama/qwen2.5",
            embedding=None,
            bsvibe_task_type=None,
            bsvibe_priority=None,
            bsvibe_complexity_hint=None,
            decision_source="rule",
        )
        # Recent rows surface via the aggregate path.
        total = await repo.usage_total(
            workspace_id=workspace_id,
            account_id=account_id,
            start=datetime.now(UTC) - timedelta(hours=1),
            end=datetime.now(UTC) + timedelta(hours=1),
        )
        assert total["total_requests"] == 1
        assert total["total_tokens"] == 100


class TestAccountScoping:
    async def test_usage_total_filters_by_account(self, session, workspace_id, account_id):
        repo = RoutingLogsRepository(session)
        other = uuid.uuid4()
        # Insert one log per account.
        for acct in (account_id, other):
            await repo.insert_routing_log(
                workspace_id=workspace_id,
                account_id=acct,
                rule_id=None,
                user_text="x",
                system_prompt="",
                features=_features(token_count=42),
                tier="local",
                strategy="static",
                score=10,
                original_model="m",
                resolved_model="m",
                embedding=None,
                bsvibe_task_type=None,
                bsvibe_priority=None,
                bsvibe_complexity_hint=None,
                decision_source=None,
            )
        own = await repo.usage_total(
            workspace_id=workspace_id,
            account_id=account_id,
            start=datetime.now(UTC) - timedelta(hours=1),
            end=datetime.now(UTC) + timedelta(hours=1),
        )
        assert own["total_requests"] == 1
        assert own["total_tokens"] == 42


class TestUsageByModel:
    async def test_groups_by_resolved_model(self, session, workspace_id, account_id):
        repo = RoutingLogsRepository(session)
        for model in ("gpt-4o", "gpt-4o", "claude-3"):
            await repo.insert_routing_log(
                workspace_id=workspace_id,
                account_id=account_id,
                rule_id=None,
                user_text="x",
                system_prompt="",
                features=_features(token_count=10),
                tier="cloud",
                strategy="static",
                score=80,
                original_model=model,
                resolved_model=model,
                embedding=None,
                bsvibe_task_type=None,
                bsvibe_priority=None,
                bsvibe_complexity_hint=None,
                decision_source=None,
            )
        rows = await repo.usage_by_model(
            workspace_id=workspace_id,
            account_id=account_id,
            start=datetime.now(UTC) - timedelta(hours=1),
            end=datetime.now(UTC) + timedelta(hours=1),
        )
        by_model = {r["resolved_model"]: r for r in rows}
        assert by_model["gpt-4o"]["request_count"] == 2
        assert by_model["claude-3"]["request_count"] == 1
