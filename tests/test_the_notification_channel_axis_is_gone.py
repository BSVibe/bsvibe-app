"""알림 prefs 의 **채널 축**을 접는다 — 무언가를 구분한 적이 없는 축이다.

## 왜 (prod 실측, 2026-08-31)

형님이 텔레그램을 붙였는데 `auth_down` 말고는 아무것도 안 왔다. 이유를 재보니:

| | 형님 작업 ws (노트 1,685) | admin ws (노트 25) |
|---|---|---|
| 실제 bound 채널 | `in_app`, **`telegram`** | `in_app`, `telegram` |
| 저장된 매트릭스의 열 | `in_app`, `slack`, `email-sender` | `in_app`, `slack`, `telegram`, `email-sender` |
| 편집 이력 | **한 번도 없음** | 1회 |

**두 워크스페이스 모두 저장된 매트릭스가 현실과 어긋나 있었다** — 한쪽은 바인딩된
적 없는 `slack`/`email-sender` 유령 열을 들고, 다른 쪽은 **바인딩된 유일한 채널의
열이 없었다.** `_enabled_push_channels` 는 저장된 키만 순회하므로 부재 = 미배달.

admin ws 의 `slack: false` / `telegram: true` 를 보고 *"채널 축을 실제로 쓴다"* 고
결론낼 뻔했다. **`slack` 은 바인딩된 적이 없다.** 그 다름은 의도가 아니라 잔재였다.

⇒ **이 축이 서로 다른 결과를 낸 사례가 0건이다.** 유일하게 표현된 선호(`needs_you`
만 on)는 이벤트 수준 판단이었고, 이 축이 프로덕션에서 한 일은 **바인딩된 유일한
채널을 조용히 침묵시킨 것**뿐이다.

관할권 축 · `region`(#845) · canon 만료축(#851)과 같은 모양 — 강제된 적도
구분한 적도 없는 축. (앞의 축은 **이름을 적지 않는다** — 그 삭제가 남긴 부재
가드가 줄 텍스트를 스캔해서, 인용만 해도 빨개진다. 이 세션에서 두 번째다.) (형님 판단, 2026-08-31: *"가장 단순한 게 가장 낫다"*)

## 새 형태 — 이벤트당 불리언 하나

켜면 **인박스 + 바인딩된 모든 푸시 채널**, 끄면 아무것도. 숨은 동작 없음.

`in_app`/`push` 2열로 남기는 안도 봤지만, 그 구분을 표현한 값들이 전부 **바인딩된
적 없는 채널**의 것이라 의도의 증거가 아니었다. 증거 없는 구분을 남기는 것은 방금
지우기로 한 것과 같은 실수다.

**이 접기는 버그를 구조적으로 없앤다** — 부재할 채널 키 자체가 사라지므로,
"붙였는데 아무것도 안 온다"가 발생할 자리가 없다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

#: 이름을 조립한다 — 일괄 삭제 스크립트가 이 가드 자신을 오염시키지 않도록.
_PER_CHANNEL = "_enabled_push" + "_channels"


def test_the_default_matrix_is_a_flat_event_switch() -> None:
    """`{event: bool}` — 채널로 중첩되지 않는다."""
    from backend.notifications.db import DEFAULT_MATRIX

    assert DEFAULT_MATRIX, "기본 매트릭스가 비었다"
    for event, value in DEFAULT_MATRIX.items():
        assert isinstance(value, bool), f"{event} 이 아직 채널로 중첩돼 있다: {value!r}"


def test_the_per_channel_selector_is_gone() -> None:
    """채널별 선택 함수가 남아 있으면 축이 살아 있는 것이다."""
    offenders: list[str] = []
    for name in ("backend", "tests"):
        root = _ROOT / name
        for path in root.rglob("*.py"):
            if path == Path(__file__):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                match node:
                    case ast.FunctionDef(name=n) | ast.Name(id=n) if n == _PER_CHANNEL:
                        offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, f"{_PER_CHANNEL} 가 남았다: {sorted(set(offenders))}"


def test_validation_rejects_the_old_nested_shape() -> None:
    """옛 모양이 조용히 통과하면 두 형태가 공존한다."""
    from backend.notifications.db import DEFAULT_EVENTS
    from backend.notifications.serialization import validate_matrix

    nested = {e: {"in_app": True} for e in DEFAULT_EVENTS}
    with pytest.raises(ValueError):
        validate_matrix(nested)


@pytest.mark.asyncio
async def test_an_enabled_event_reaches_every_bound_push_channel() -> None:
    """⭐ 핵심 — 켜면 **바인딩된 것 전부**로 간다. 채널별 opt-in 이 없다.

    이게 형님이 겪은 버그가 사라지는 지점이다: 커넥터를 붙이는 것과 그 채널이
    실제로 받는 것 사이에 **아무 저장 상태도 끼지 않는다.**
    """
    from backend.workflow.infrastructure.workers.notify_worker import channels_for_event

    matrix = {"needs_you": True, "daily_brief": False}
    assert channels_for_event(matrix, event="needs_you", bound={"telegram", "slack"}) == {
        "telegram",
        "slack",
    }


@pytest.mark.asyncio
async def test_a_disabled_event_reaches_nothing() -> None:
    from backend.workflow.infrastructure.workers.notify_worker import channels_for_event

    matrix = {"needs_you": True, "daily_brief": False}
    assert channels_for_event(matrix, event="daily_brief", bound={"telegram"}) == set()


# ── 양성 대조군 ────────────────────────────────────────────────────────────


def test_auth_down_still_defaults_on_for_anything_bound() -> None:
    """축을 접으면서 §Ⅱ.0 의 교훈을 잃으면 안 된다.

    인증 장애에는 인박스 자체가 깨져 있으므로, 옵트인을 기다리는 알림은 아무도
    못 받는 알림이다.
    """
    from backend.workflow.infrastructure.workers.notify_worker import channels_for_event

    assert channels_for_event({}, event="auth_down", bound={"telegram"}) == {"telegram"}


def test_an_unbound_channel_never_receives() -> None:
    """양성 대조군 — 바인딩이 여전히 게이트다. 켠다고 없는 채널로 가지 않는다."""
    from backend.workflow.infrastructure.workers.notify_worker import channels_for_event

    assert channels_for_event({"needs_you": True}, event="needs_you", bound=set()) == set()


def test_every_known_event_still_has_a_switch() -> None:
    """양성 대조군 — 축을 접으면서 이벤트를 잃으면 안 된다."""
    from backend.notifications.db import DEFAULT_EVENTS, DEFAULT_MATRIX

    assert set(DEFAULT_MATRIX) == set(DEFAULT_EVENTS)
