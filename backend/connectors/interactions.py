"""어떤 커넥터가 **버튼 탭 승인**을 받을 수 있나 — 그 등록부의 단 하나의 자리.

세 콜백 구현은 처음부터 이 컨텍스트에 살았다 (``telegram_callback`` ·
``slack_callback`` · ``discord_callback``). 그런데 **등록부만** ``api/webhooks.py``
안의 ``_INTERACTION_CALLBACKS`` 딕셔너리로 있었다.

그 결과 :class:`~backend.connectors.catalog.ConnectorInfo` 가 *"Every field is
derived … there is no hand-maintained second copy"* 라고 선언해둔 축 밖에서,
네 번째 능력이 손으로 유지됐다 — 카탈로그는 "이 커넥터가 승인 탭을 받는가"를
모르고, 어느 표면도 그것을 보여줄 수 없었다.

등록부를 여기로 옮기고 카탈로그가 :data:`INTERACTION_CONNECTORS` 에서 **파생**한다.

⚠️ **지연 import 규율(R2c)은 그대로다.** 콜백 모듈들은 ``plugin.*`` 에 닿으므로
최상위에서 import 하지 않는다 — :func:`interaction_callback` 이 호출 시점에
가져온다. 그래야 ``backend.api.webhooks`` 가 ``plugin.*`` 정적 엣지 0을 유지하고,
테스트가 핸들러를 호출 시점에 monkeypatch 할 수 있다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

InteractionCallback = Callable[..., Awaitable[Any]]

#: 버튼 탭 승인을 받는 커넥터. 추가는 한 줄 등록이다.
INTERACTION_CONNECTORS = frozenset({"telegram", "slack", "discord"})

__all__ = ["INTERACTION_CONNECTORS", "InteractionCallback", "interaction_callback"]


def interaction_callback(connector: str) -> InteractionCallback | None:
    """``connector`` 의 인터랙티브 승인 진입점 (없으면 ``None``).

    import 는 **호출 시점**이다 — 위 R2c 규율 참조.
    """
    if connector == "telegram":
        from backend.connectors.telegram_callback import (  # noqa: PLC0415
            process_telegram_callback,
        )

        return process_telegram_callback
    if connector == "slack":
        from backend.connectors.slack_callback import (  # noqa: PLC0415
            process_slack_callback,
        )

        return process_slack_callback
    if connector == "discord":
        from backend.connectors.discord_callback import (  # noqa: PLC0415
            process_discord_callback,
        )

        return process_discord_callback
    return None
