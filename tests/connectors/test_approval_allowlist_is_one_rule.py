"""승인 권한은 **한 규칙**이다 — 커넥터마다 사본을 두지 않는다.

2026-09-23 실측: 슬랙과 디스코드의 허용목록 판정은 **글자까지 같았다**. 다른 건
스코프 키 하나뿐이다(``team_id`` vs ``guild_id``). 텔레그램만 전혀 다른 등식
(``chat_type == "private"`` AND ``from_id == chat_id``)을 써서 **그룹방에서 승인이
불가능**했고, 그게 "1:1 과 그룹을 구분해야 한다"는 제약의 유일한 출처였다.

형님 결정(2026-09-23): 셋을 같은 정책으로 통일한다. 목적은 **구분을 없애는 것**이다.

⚠️ 형님이 처음 말씀하신 *"방에 있으면 권한"* 은 채택하지 **않았다** — 슬랙 코드가
그걸 이미 기각해 뒀다(*"a channel card is tappable by any member"*). 허용목록은
채팅 종류와 무관하므로, 그것만으로 그룹방이 열리고 구분도 사라진다.

이 모듈은 **규칙이 하나임**을 매 런 단언한다. 사본이 다시 생기면 그게 어긋나고,
이 레포는 미러가 어긋나 응답에 라이브 크리덴셜을 낸 전력이 있다.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest


def _callback_modules() -> dict[str, Any]:
    """승인 콜백을 가진 커넥터 모듈 전수 — **디렉터리에서** 센다.

    하드코딩 목록은 내가 아는 커넥터만 증명한다. 네 번째 커넥터가 추가되면
    자기 사본을 들고 와도 이 집합에 들어와 아래 단언에 걸린다.
    """
    import importlib

    root = Path(__file__).resolve().parents[2] / "backend/connectors"
    mods = {}
    for f in sorted(root.glob("*_callback.py")):
        m = importlib.import_module(f"backend.connectors.{f.stem}")
        if any(n.startswith("_is_authorized") for n in vars(m)):
            mods[f.stem] = m
    return mods


def test_the_connector_set_is_what_i_think_it_is() -> None:
    """크기 단언 — 집합이 비면 아래 전부가 공허하게 통과한다."""
    assert set(_callback_modules()) == {
        "discord_callback",
        "slack_callback",
        "telegram_callback",
    }, sorted(_callback_modules())


def test_every_connector_delegates_to_the_one_rule() -> None:
    """사본이 아니라 **같은 함수**를 부른다.

    소스를 grep 하는 게 아니라 각 판정 함수의 소스에 공유 함수 이름이 있는지 본다 —
    이름을 바꾸면 여기가 빨개지고, 그게 의도된 신호다.
    """
    from backend.common.approval_allowlist import is_authorized_tapper

    offenders = []
    for name, mod in _callback_modules().items():
        fn = next(v for k, v in vars(mod).items() if k.startswith("_is_authorized"))
        if is_authorized_tapper.__name__ not in inspect.getsource(fn):
            offenders.append(name)
    assert not offenders, f"허용목록 사본을 들고 있다: {offenders}"


@pytest.mark.parametrize(
    ("config", "user_id", "scope", "expected"),
    [
        ({"authorized_user_ids": ["7"]}, "7", None, True),
        ({"authorized_user_ids": [7]}, "7", None, True),  # int/str 혼용 허용
        ({"authorized_user_ids": ["7"]}, "8", None, False),
        ({}, "7", None, False),  # 키 없음 = 아무도
        ({"authorized_user_ids": []}, "7", None, False),  # 빈 목록 = 아무도
        ({"authorized_user_ids": "7"}, "7", None, False),  # 리스트가 아님
        ({"authorized_user_ids": ["7"]}, None, None, False),
        # 스코프 바인딩: 묶여 있으면 일치해야 한다
        ({"authorized_user_ids": ["7"], "team_id": "T1"}, "7", "T1", True),
        ({"authorized_user_ids": ["7"], "team_id": "T1"}, "7", "T2", False),
        ({"authorized_user_ids": ["7"]}, "7", "T2", True),  # 안 묶였으면 무관
    ],
)
def test_the_rule_itself(
    config: dict, user_id: str | None, scope: str | None, expected: bool
) -> None:
    """FAIL-CLOSED 가 양방향으로 뒤집히는지 — 전부 거절하는 구현도 통과하면 안 된다."""
    from backend.common.approval_allowlist import is_authorized_tapper

    assert (
        is_authorized_tapper(
            delivery_config=config, user_id=user_id, scope_key="team_id", scope_value=scope
        )
        is expected
    )


def test_the_rule_does_not_read_chat_type() -> None:
    """1:1/그룹 구분을 없앤 것이 이 변경의 요점이다.

    규칙이 채팅 종류를 다시 보기 시작하면 텔레그램만 또 갈라진다.
    """
    from backend.common.approval_allowlist import is_authorized_tapper

    # 모듈 독스트링은 **왜** 그 조건이 사라졌는지를 적고 있고 그건 남아야 한다.
    # 명제는 "규칙이 그걸 읽지 않는다" 이므로 함수 소스만 본다.
    source = inspect.getsource(is_authorized_tapper)
    assert "chat_type" not in source
    assert "private" not in source
    # 대조군 — 함수 소스를 실제로 읽고 있다(빈 문자열이면 위가 공허하게 통과한다)
    assert "authorized_user_ids" in source
