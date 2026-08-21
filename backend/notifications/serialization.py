"""알림 설정(prefs)의 요청/응답 모양과 검증 — 단 하나의 자리.

이 조각들은 원래 ``api/v1/notifications.py`` 와 ``mcp/tools/notifications_tools.py``
**양쪽**에 있었고, MCP 쪽이 스스로 적어뒀다 — *"mirrors REST ``PrefsBody`` 1:1"* ·
*"Same validator the REST surface applies"*. 정규식 · 이벤트 집합 · 검증기 · 뷰 두 종 ·
직렬화까지 **여섯 조각**이 두 벌이었다. 새 이벤트가 하나 늘면 두 곳을 다 고쳐야 했다.

⚠️ **오류 표면은 여기서 정하지 않는다.** 검증 실패는 ``ValueError`` 로 올라가고,
그것이 HTTP 422 가 되는지 ``ToolError`` 가 되는지는 각 프로토콜의 몫이다.

``backend.notifications`` 는 MCP 의 import 계약 금지 목록에 없어서, 양쪽이 예외 없이
의존한다.
"""

from __future__ import annotations

import re
import uuid

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.notifications.db import (
    DEFAULT_EVENTS,
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
    NotificationPrefsRow,
    default_matrix,
)

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EVENT_SET = frozenset(DEFAULT_EVENTS)

__all__ = [
    "EVENT_SET",
    "HHMM",
    "PrefsBody",
    "PrefsView",
    "get_or_create_prefs",
    "prefs_view_from_row",
    "validate_matrix",
]


def validate_matrix(matrix: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
    """Validate the matrix: exactly the known events; any channel keys tolerated.

    Events are still the fixed :data:`DEFAULT_EVENTS`. Channels are NOT fixed —
    they are derived per workspace from connector bindings, so the validator
    accepts any subset of channel keys (a stale key for a since-removed connector
    is harmless — ignored at send time — rather than rejected). Values must be
    booleans.
    """
    if set(matrix.keys()) != EVENT_SET:
        raise ValueError(
            f"matrix events must be exactly {sorted(EVENT_SET)}; got {sorted(matrix.keys())}"
        )
    for event_id, channels in matrix.items():
        for channel_id, enabled in channels.items():
            if not isinstance(enabled, bool):
                raise ValueError(
                    f"matrix[{event_id!r}][{channel_id!r}] must be a bool; got {enabled!r}"
                )
    return matrix


class PrefsBody(BaseModel):
    """Shared request/response shape for the prefs surface."""

    model_config = ConfigDict(extra="forbid")

    matrix: dict[str, dict[str, bool]]
    quiet_hours_enabled: bool
    quiet_hours_start: str
    quiet_hours_end: str

    @field_validator("matrix")
    @classmethod
    def _check_matrix(cls, v: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
        return validate_matrix(v)

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def _check_time(cls, v: str) -> str:
        if not HHMM.match(v):
            raise ValueError(f"quiet-hours time must be HH:MM (00:00-23:59); got {v!r}")
        return v


class PrefsView(PrefsBody):
    """Read/write response — the stored prefs plus the derived channel columns.

    ``available_channels`` is the workspace's live notification channels
    (``in_app`` + every bound notify-channel connector), recomputed at read time
    from connector bindings. It is response-only: the PWA renders the matrix
    columns from it, and it is not settable (a PUT that echoes it back is
    rejected by ``extra=forbid`` on :class:`PrefsBody`).
    """

    available_channels: list[str]


async def get_or_create_prefs(
    session: AsyncSession, workspace_id: uuid.UUID
) -> NotificationPrefsRow:
    """이 워크스페이스의 prefs 행 — 없으면 기본값으로 만들어 준다.

    양쪽 표면이 **바이트 동일하게** 갖고 있던 함수다. 기본 matrix / quiet-hours 를
    한 곳에서만 정하도록 여기로 모았다.
    """
    row = (
        await session.execute(
            select(NotificationPrefsRow).where(NotificationPrefsRow.workspace_id == workspace_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = NotificationPrefsRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            matrix=default_matrix(),
            quiet_hours_enabled=False,
            quiet_hours_start=DEFAULT_QUIET_HOURS_START,
            quiet_hours_end=DEFAULT_QUIET_HOURS_END,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


def prefs_view_from_row(row: NotificationPrefsRow, channels: list[str]) -> PrefsView:
    """행 + 파생된 채널 목록 → 응답 모양."""
    return PrefsView(
        matrix=row.matrix,
        quiet_hours_enabled=row.quiet_hours_enabled,
        quiet_hours_start=row.quiet_hours_start,
        quiet_hours_end=row.quiet_hours_end,
        available_channels=channels,
    )
