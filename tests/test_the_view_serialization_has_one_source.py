"""감사 C5 · C7 — 두 표면이 뷰 모델과 검증기를 손으로 미러했다.

두 미러 모두 **자기가 미러라고 적어뒀다**:

* C5 — ``mcp/tools/notifications_tools.py``: *"mirrors REST ``PrefsBody`` 1:1"* ·
  *"Same validator the REST surface applies"*. 검증기 본문 · 정규식 · 이벤트 집합 ·
  뷰 두 종 · 직렬화까지 **여섯 조각**이 두 벌이었다.
* C7 — ``mcp/tools/schedule_tools.py``: ``ScheduleView`` 와 ``_to_view`` 가 REST 와
  같은 모양으로 다시 적혀 있었다.

## 어디에 두는가 — 형님 판단 2026-08-21

**계층 분리를 우선한다.** 각 소유 컨텍스트에 ``serialization`` 모듈을 따로 만든다.
C7 은 ``backend.schedule`` 이 MCP 금지 컨텍스트라 import-linter 예외가 **하나 늘지만**,
SQLAlchemy 모듈에 Pydantic 뷰를 섞지 않는 쪽을 택했다.

===  ==============================================  ============
건    위치                                             예외 순증
===  ==============================================  ============
C5   ``backend/notifications/serialization.py``       **0** (notifications 는 MCP 금지 목록에 없다)
C7   ``backend/schedule/serialization.py``            **+1**
===  ==============================================  ============

## ⚠️ 오류 표면은 합치지 않는다

C10 에서 세운 규칙 그대로다 — 검증 **규칙**은 공유하되, 그것이 HTTP 422 가 되는지
``ToolError`` 가 되는지는 프로토콜의 몫이다. 공유 모델은 ``ValueError`` 를 올리고
각 표면이 자기 오류로 옮긴다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backend"


def _sites(needle: str) -> list[str]:
    return [
        f"{p.relative_to(_ROOT)}:{i}"
        for p in _BACKEND.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if needle in line
    ]


# ── C5 ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("needle", "owner"),
    [
        ("def validate_matrix", "backend/notifications/"),
        ('HHMM = re.compile(r"^([01]\\d|2[0-3]):[0-5]\\d$")', "backend/notifications/"),
        ("class PrefsBody", "backend/notifications/"),
        ("class PrefsView", "backend/notifications/"),
    ],
)
def test_the_prefs_piece_is_declared_once_in_its_owning_context(needle: str, owner: str) -> None:
    sites = _sites(needle)
    assert len(sites) == 1, f"{needle!r} 이 여러 곳에 있다: {sites}"
    assert sites[0].startswith(owner), f"소유 컨텍스트가 아니다: {sites[0]}"


def test_both_prefs_surfaces_share_the_same_model() -> None:
    """특성화 — REST 와 MCP 가 **같은 클래스 객체**를 쓴다."""
    from backend.api.v1 import notifications as rest
    from backend.mcp.tools import notifications_tools as mcp
    from backend.notifications.serialization import (
        HHMM,
        PrefsBody,
        PrefsView,
        validate_matrix,
    )

    assert rest.PrefsBody is PrefsBody
    assert rest.PrefsView is PrefsView
    assert mcp._PrefsView is PrefsView  # noqa: SLF001
    # MCP 는 자기 입력 모델(``NotificationPrefsUpdateInput``)을 따로 갖고 있어
    # ``PrefsBody`` 를 직접 쓰지 않는다 — 공유되는 것은 **검증 규칙**이다.
    assert mcp._validate_matrix is validate_matrix  # noqa: SLF001
    assert mcp._HHMM is HHMM  # noqa: SLF001


def test_the_matrix_validator_still_rejects_a_wrong_event_set() -> None:
    """특성화 — 통합 후에도 검증이 그대로여야 한다."""
    from backend.notifications.serialization import validate_matrix

    with pytest.raises(ValueError, match="matrix events must be exactly"):
        validate_matrix({"not-an-event": {"in_app": True}})


def test_the_matrix_validator_still_rejects_a_non_bool() -> None:
    from backend.notifications.db import DEFAULT_EVENTS
    from backend.notifications.serialization import validate_matrix

    bad = {e: {"in_app": True} for e in DEFAULT_EVENTS}
    bad[next(iter(DEFAULT_EVENTS))] = {"in_app": "yes"}  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="must be a bool"):
        validate_matrix(bad)


def test_quiet_hours_must_be_hhmm() -> None:
    from backend.notifications.db import DEFAULT_EVENTS
    from backend.notifications.serialization import PrefsBody

    ok = {e: {"in_app": True} for e in DEFAULT_EVENTS}
    with pytest.raises(ValueError, match="HH:MM"):
        PrefsBody(
            matrix=ok,
            quiet_hours_enabled=True,
            quiet_hours_start="25:00",
            quiet_hours_end="07:00",
        )


# ── C7 ────────────────────────────────────────────────────────────────────


def test_the_schedule_view_is_declared_once_in_its_owning_context() -> None:
    sites = _sites("class ScheduleView")
    assert len(sites) == 1, f"ScheduleView 가 여러 곳에 있다: {sites}"
    assert sites[0].startswith("backend/schedule/"), f"소유 컨텍스트가 아니다: {sites[0]}"


def test_both_schedule_surfaces_share_the_same_model() -> None:
    from backend.api.v1 import schedules as rest
    from backend.mcp.tools import schedule_tools as mcp
    from backend.schedule.serialization import ScheduleView

    assert rest.ScheduleView is ScheduleView
    assert mcp.ScheduleView is ScheduleView


def test_the_schedule_view_keeps_the_payload_text_fallback() -> None:
    """특성화 — ``payload["text"]`` 가 문자열이 아니면 빈 문자열로 떨어진다.

    두 표면이 각자 적어둔 그 방어를 통합이 잃으면 ``None`` 이 새어 나간다."""
    import uuid

    from backend.schedule.serialization import schedule_view_from_row

    class _Row:
        id = uuid.uuid4()
        kind = "direct"
        payload: dict[str, object] = {"text": None}
        cron_expr = "0 9 * * 1"
        product_id = None
        title = None
        next_run_at = None
        last_fired_at = None
        enabled = True

    assert schedule_view_from_row(_Row()).text == ""  # type: ignore[arg-type]


# ── 계약 ──────────────────────────────────────────────────────────────────


def test_the_exception_ledger_grew_by_exactly_one() -> None:
    """형님 판단 — 계층 분리를 위해 예외 **하나**만 늘린다.

    C5 는 ``backend.notifications`` 가 MCP 금지 목록에 없어 예외가 필요 없다."""
    toml = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"backend.mcp.tools.schedule_tools -> backend.schedule.serialization"' in toml
    assert "notifications_tools -> backend.notifications.serialization" not in toml
