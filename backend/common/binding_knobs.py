"""바인딩의 ``trigger`` 노브 — REST 와 MCP 가 **같은 클래스**를 쓰는 자리.

#924 가 ``trigger.enabled`` 를 지웠다. REST 는 :class:`TriggerKnob` 에
``extra="forbid"`` 를 걸어 옛 클라이언트를 거절하게 닫혔지만, MCP 쪽 입력은
``trigger: dict[str, Any]`` 인 채로 남았다 — 스스로는 독스트링에
*"Mirror of ``ResourceBindingCreate`` … accepts it 1:1"* 이라고 적어 두고서.

그 어긋남이 실제로 값을 치렀다. prod 의 09-18 바인딩은 **삭제 일주일 뒤에**
만들어졌는데도 ``{"enabled": false, "filters": {}}`` 를 들고 있다 — PWA 는
``trigger`` 를 안 보내고 REST 는 거절하므로, 열려 있던 문은 MCP 하나뿐이었다.

:mod:`backend.common.connector_redaction` 이 **똑같은 이유로** 여기 있다(미러가
어긋나 응답에 라이브 크리덴셜이 나갔다). 순수 pydantic 모델이라 leaf 에 두면
MCP 가 import 계약 예외 없이 쓴다 — 소유 컨텍스트에 두면 예외를 파야 하고,
예외는 이 모듈이 없애려는 종류의 부채다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TriggerKnob(BaseModel):
    """``{"filters": dict}`` — the *do I act* knob.

    ``filters`` is a dict of key-equality checks the Receive stage applies to
    the inbound payload; an empty dict acts on everything. No other key is
    accepted (``extra="forbid"``).

    ⚠️ **이 독스트링은 MCP 의 와이어 스키마 설명이 된다** — 에이전트가 읽는
    메뉴다. 폐기된 키의 내력은 위 모듈 독스트링에 적어라. 여기에 적으면
    (거절한다고 적더라도) 그 이름이 메뉴로 되돌아간다.
    """

    model_config = ConfigDict(extra="forbid")

    filters: dict[str, Any] = Field(default_factory=dict)


__all__ = ["TriggerKnob"]
