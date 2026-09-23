"""승인 탭의 **인간 권한** 판정 — 세 커넥터가 공유하는 단 하나의 규칙.

2026-09-23 이전에는 이 규칙이 세 벌이었다. 슬랙과 디스코드는 허용목록 절반이
**글자까지 같았고**(다른 건 스코프 키 하나: ``team_id`` vs ``guild_id``),
텔레그램만 전혀 다른 등식을 썼다 —

    chat_type == "private" AND from_id == delivery_config["chat_id"]

그 등식이 1:1 에서만 성립했기 때문에 **그룹방에서는 승인이 아예 불가능**했고,
그게 *"그룹 = 배송 전용, 승인 = 1:1 전용"* 이라는 제약의 유일한 출처였다.
형님 결정(2026-09-23)으로 셋을 통일하면서 그 제약이 사라진다.

⚠️ **"방에 있으면 권한" 은 채택하지 않았다.** 슬랙 구현이 그 안을 이미 기각해
뒀다 — *"approval is irreversible, and a channel card is tappable by any
member"*. 허용목록은 채팅 종류와 무관하므로 그룹방을 여는 데 그게 필요하지도
않다. 여는 것은 **누가 눌렀는지를 본다**는 사실이지 방의 성격이 아니다.

:mod:`backend.common.connector_redaction` 이 같은 이유로 여기 있다 — 미러가
어긋나 응답에 라이브 크리덴셜이 나간 전력. ``dict`` 와 ``str`` 만 다루는 순수
함수라 leaf 에 두면 커넥터들이 import 계약 예외 없이 쓴다.
"""

from __future__ import annotations

from typing import Any

__all__ = ["is_authorized_tapper"]


def is_authorized_tapper(
    *,
    delivery_config: dict[str, Any],
    user_id: Any,
    scope_key: str | None = None,
    scope_value: Any = None,
) -> bool:
    """탭한 사람이 승인 권한을 가졌는가.

    ``user_id`` 가 계정의 ``authorized_user_ids`` 허용목록에 있고, ``scope_key``
    가 계정에 묶여 있다면 ``scope_value`` 가 그것과 일치할 때만 참이다.

    **FAIL-CLOSED**: 허용목록이 없거나 비었으면 **아무도** 승인할 수 없다.
    승인은 되돌릴 수 없고, 카드는 그 방의 아무 멤버나 누를 수 있기 때문이다.
    이건 사람 권한 층이다 — 전송 서명은 이 함수가 불리기 전에 이미 검증됐다.

    ``delivery_config`` 는 int 와 str 을 섞어 저장할 수 있으므로 **문자열로**
    비교한다(텔레그램 chat id 는 int, 슬랙 user id 는 str 이다).
    """
    allowed = delivery_config.get("authorized_user_ids")
    if not isinstance(allowed, (list, tuple)) or not allowed:
        return False
    if user_id is None or str(user_id) not in {str(u) for u in allowed}:
        return False
    if scope_key is not None:
        bound = delivery_config.get(scope_key)
        if bound is not None and str(scope_value) != str(bound):
            return False
    return True
