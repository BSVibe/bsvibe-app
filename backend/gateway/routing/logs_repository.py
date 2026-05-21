"""SQL CRUD for ``routing_logs``, account-scoped.

Centralizes every read and write so the ``(workspace_id, account_id)``
isolation invariant cannot be bypassed by ad-hoc SQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.gateway.routing.db import RoutingLogRow


@dataclass(frozen=True)
class RoutingLogFeatures:
    """Per-request features captured alongside the routing decision.

    Stored verbatim on the log row so downstream analytics can re-derive
    classifier inputs without re-parsing the messages.
    """

    token_count: int
    conversation_turns: int
    code_block_count: int
    code_lines: int
    has_error_trace: bool
    tool_count: int


class RoutingLogsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_routing_log(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        rule_id: uuid.UUID | None,
        user_text: str,
        system_prompt: str,
        features: RoutingLogFeatures,
        tier: str,
        strategy: str,
        score: int | None,
        original_model: str,
        resolved_model: str,
        embedding: list[float] | None,
        bsvibe_task_type: str | None,
        bsvibe_priority: str | None,
        bsvibe_complexity_hint: int | None,
        decision_source: str | None,
    ) -> None:
        row = RoutingLogRow(
            workspace_id=workspace_id,
            account_id=account_id,
            rule_id=rule_id,
            user_text=user_text,
            system_prompt=system_prompt,
            token_count=features.token_count,
            conversation_turns=features.conversation_turns,
            code_block_count=features.code_block_count,
            code_lines=features.code_lines,
            has_error_trace=features.has_error_trace,
            tool_count=features.tool_count,
            tier=tier,
            strategy=strategy,
            score=score,
            original_model=original_model,
            resolved_model=resolved_model,
            embedding=embedding,
            bsvibe_task_type=bsvibe_task_type,
            bsvibe_priority=bsvibe_priority,
            bsvibe_complexity_hint=bsvibe_complexity_hint,
            decision_source=decision_source,
        )
        self._session.add(row)
        await self._session.flush()

    async def usage_total(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        start: datetime,
        end: datetime,
    ) -> dict[str, int]:
        stmt = select(
            func.count(RoutingLogRow.id).label("total_requests"),
            func.coalesce(func.sum(RoutingLogRow.token_count), 0).label("total_tokens"),
        ).where(
            RoutingLogRow.workspace_id == workspace_id,
            RoutingLogRow.account_id == account_id,
            RoutingLogRow.timestamp >= start,
            RoutingLogRow.timestamp < end,
        )
        row = (await self._session.execute(stmt)).one()
        return {
            "total_requests": int(row.total_requests or 0),
            "total_tokens": int(row.total_tokens or 0),
        }

    async def usage_by_model(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        start: datetime,
        end: datetime,
    ) -> Sequence[dict[str, Any]]:
        stmt = (
            select(
                RoutingLogRow.resolved_model,
                func.count(RoutingLogRow.id).label("request_count"),
                func.coalesce(func.sum(RoutingLogRow.token_count), 0).label("token_count"),
            )
            .where(
                RoutingLogRow.workspace_id == workspace_id,
                RoutingLogRow.account_id == account_id,
                RoutingLogRow.timestamp >= start,
                RoutingLogRow.timestamp < end,
            )
            .group_by(RoutingLogRow.resolved_model)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            {
                "resolved_model": r.resolved_model,
                "request_count": int(r.request_count or 0),
                "token_count": int(r.token_count or 0),
            }
            for r in rows
        ]

    async def usage_by_rule(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        start: datetime,
        end: datetime,
    ) -> Sequence[dict[str, Any]]:
        stmt = (
            select(
                RoutingLogRow.rule_id,
                func.count(RoutingLogRow.id).label("request_count"),
            )
            .where(
                RoutingLogRow.workspace_id == workspace_id,
                RoutingLogRow.account_id == account_id,
                RoutingLogRow.timestamp >= start,
                RoutingLogRow.timestamp < end,
            )
            .group_by(RoutingLogRow.rule_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            {
                "rule_id": r.rule_id,
                "request_count": int(r.request_count or 0),
            }
            for r in rows
        ]
