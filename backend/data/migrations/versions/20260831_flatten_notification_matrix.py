"""알림 prefs 매트릭스에서 채널 축을 접는다 — ``{event: {channel: bool}}`` → ``{event: bool}``.

그 축은 무언가를 구분한 적이 없다. prod 실측(2026-08-31): 한 워크스페이스는
바인딩된 적 없는 ``slack``/``email-sender`` 열을 들고 있었고, 다른 하나는 **바인딩된
유일한 채널(``telegram``)의 열이 없었다.** 보내는 쪽은 저장된 키만 순회하므로,
형님이 텔레그램을 붙이고도 ``auth_down`` 말고는 아무것도 못 받았다.

## 값을 무엇으로 접나 — ``in_app``

푸시 채널들의 값(``slack``/``email-sender``)은 **전부 바인딩된 적 없는 채널**의
것이라 의도의 증거가 아니다. OR 로 접으면 ``email-sender: true`` 때문에 모든
이벤트가 켜진다(``daily_brief`` 포함) — 아무도 고른 적 없는 결과다.

``in_app`` 은 그 워크스페이스가 **실제로 도달하는 채널**에 대해 내린 유일한 판단이다.
그것을 이벤트의 스위치로 승격한다.

Revision ID: flatten_notification_matrix
Revises: drop_workspace_region
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "flatten_notification_matrix"
down_revision = "drop_workspace_region"
branch_labels = None
depends_on = None

_EVENTS = ("needs_you", "triggered", "shipped", "failed", "daily_brief", "auth_down")
#: 새 행이 받는 기본값 — ``DEFAULT_MATRIX`` 와 같아야 한다.
_DEFAULTS = {e: e != "daily_brief" for e in _EVENTS}


def _rows(conn):
    return conn.execute(sa.text("select id, matrix from notification_prefs")).fetchall()


def _loaded(raw):
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def upgrade() -> None:
    conn = op.get_bind()
    for row_id, raw in _rows(conn):
        stored = _loaded(raw)
        flat: dict[str, bool] = {}
        for event in _EVENTS:
            value = stored.get(event)
            if isinstance(value, dict):
                # 옛 모양 — ``in_app`` 을 승격한다(위 docstring 참조).
                flat[event] = bool(value.get("in_app", _DEFAULTS[event]))
            elif isinstance(value, bool):
                flat[event] = value  # 이미 접힌 행 (재실행 안전)
            else:
                flat[event] = _DEFAULTS[event]
        conn.execute(
            sa.text("update notification_prefs set matrix = :m where id = :i"),
            {"m": json.dumps(flat), "i": str(row_id)},
        )


def downgrade() -> None:
    """평면 스위치를 ``{"in_app": <값>}`` 으로 되돌린다.

    채널별 선택은 **복원할 수 없다** — 접을 때 버려졌고, 애초에 바인딩된 적 없는
    채널의 값이었다. 되돌린 행은 옛 스키마에서 유효하며 인박스 판단만 보존한다.
    """
    conn = op.get_bind()
    for row_id, raw in _rows(conn):
        stored = _loaded(raw)
        nested = {
            event: (
                {"in_app": bool(stored.get(event, _DEFAULTS[event]))}
                if not isinstance(stored.get(event), dict)
                else stored[event]
            )
            for event in _EVENTS
        }
        conn.execute(
            sa.text("update notification_prefs set matrix = :m where id = :i"),
            {"m": json.dumps(nested), "i": str(row_id)},
        )
