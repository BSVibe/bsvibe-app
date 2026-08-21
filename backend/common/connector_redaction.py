"""커넥터 행을 **응답용으로** 깎는 단 하나의 자리.

이 모듈이 생기기 전에는 ``backend/api/v1/connectors.py`` 와
``backend/mcp/tools/connectors_tools.py`` 가 시크릿 키 목록과 마스킹 함수를
각각 갖고 있었다. MCP 쪽 주석이 그 대가를 적어뒀다 —

    *"Mirrors the REST ``_SECRET_DELIVERY_KEYS`` … (**previously the MCP
    serializer echoed these unredacted**)."*

미러라서 한 번 어긋났고, 어긋난 결과가 **응답에 라이브 크리덴셜 노출**이었다.
새 커넥터가 시크릿 키를 하나 더 들고 오면 두 곳을 다 고쳐야 하는 구조였다.

여기는 ``dict`` 와 ``str`` 만 다루는 순수 함수라 ``backend.common`` leaf 에 둔다 —
그러면 MCP 가 import 계약 예외 없이 쓸 수 있다. 소유 컨텍스트(``backend.connectors``)에
두면 예외를 하나 더 파야 하는데, 그건 이 리팩터가 없애려는 종류의 부채다.
:mod:`backend.common.settle_kinds` 와 :mod:`backend.common.slug` 가 같은 이유로 여기 있다.
"""

from __future__ import annotations

from typing import Any

#: 시크릿을 담아 **응답으로 절대 나가면 안 되는** ``delivery_config`` 키.
#:
#: inbound ``webhook_secret`` 은 형님이 외부 제공자 콘솔에서 붙여 넣는 서명 시크릿이다
#: — 제공자가 *우리에게* 자신을 증명하는 값이라, 목록/생성/패치 응답에 그대로 실으면
#: 살아 있는 크리덴셜이 PWA(그리고 그 JSON 을 읽는 누구에게나)로 샌다.
#: 마스킹은 **응답 쪽에서만** 한다 — 저장된 행은 키를 그대로 갖고 있어야 ingress 가
#: 서명을 계속 검증할 수 있다.
SECRET_DELIVERY_KEYS = frozenset({"webhook_secret", "signing_secret", "client_secret"})

__all__ = ["SECRET_DELIVERY_KEYS", "public_delivery_config", "token_hint"]


def public_delivery_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``cfg`` with secret-bearing keys dropped (response-side only)."""
    return {k: v for k, v in cfg.items() if k not in SECRET_DELIVERY_KEYS}


def token_hint(webhook_token: str) -> str:
    """Last 4 chars only — enough to recognise, not enough to use."""
    return f"...{webhook_token[-4:]}"
