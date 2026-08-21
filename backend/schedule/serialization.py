"""워크스페이스 스케줄의 응답 모양 — 단 하나의 자리.

``ScheduleView`` 와 그 직렬화가 ``api/v1/schedules.py`` 와
``mcp/tools/schedule_tools.py`` **양쪽**에 같은 모양으로 적혀 있었다.

여기 두는 것은 형님 판단(2026-08-21)이다 — ``backend.schedule`` 은 MCP 의 import
계약에서 금지 컨텍스트라 ``schedule_tools -> backend.schedule.serialization`` 예외가
**하나 늘어난다.** 이미 예외가 걸린 ``schedule_db`` 안에 넣으면 예외는 안 늘지만
SQLAlchemy 모듈에 Pydantic 뷰가 섞인다 — **계층 분리를 우선**했다.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

__all__ = ["ScheduleRowLike", "ScheduleView", "schedule_view_from_row"]


class ScheduleView(BaseModel):
    """One workspace schedule, as both surfaces render it."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    kind: str
    text: str
    cron_expr: str
    product_id: uuid.UUID | None
    title: str | None
    next_run_at: datetime | None
    last_fired_at: datetime | None
    enabled: bool


class ScheduleRowLike(Protocol):
    """``WorkspaceScheduleRow`` 가 만족하는 구조 — ORM 을 여기서 import 하지 않는다.

    ⚠️ Protocol 속성은 **불변(invariant)** 이라 실제 컬럼 타입과 정확히 같아야 한다.
    ``payload`` 와 ``next_run_at`` 은 DB 에서 NOT NULL 이다 — ``| None`` 을 붙였다가
    ``mypy --strict`` 에 잡혔다. 그래도 ``payload`` 는 런타임에 비어 있을 수 있어
    :func:`schedule_view_from_row` 가 방어한다.
    """

    id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    cron_expr: str
    product_id: uuid.UUID | None
    title: str | None
    next_run_at: datetime
    last_fired_at: datetime | None
    enabled: bool


def schedule_view_from_row(row: ScheduleRowLike) -> ScheduleView:
    """행을 응답 모양으로.

    ``payload["text"]`` 가 문자열이 아니면 **빈 문자열**로 떨어진다 — 두 표면이 각자
    적어뒀던 방어다. 없애면 ``None`` 이 ``text: str`` 자리로 새어 나간다.
    """
    payload: dict[str, Any] = row.payload or {}
    text_value = payload.get("text")
    return ScheduleView(
        id=row.id,
        kind=row.kind,
        text=text_value if isinstance(text_value, str) else "",
        cron_expr=row.cron_expr,
        product_id=row.product_id,
        title=row.title,
        next_run_at=row.next_run_at,
        last_fired_at=row.last_fired_at,
        enabled=row.enabled,
    )
