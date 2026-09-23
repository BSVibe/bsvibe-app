"""MCP 쓰기가 **실제로 감사 행을 남기는가** (#1039).

2026-09-23 실측. 49개 쓰기 툴이 ``audit_event`` 를 선언하는데 prod 의
``audit_outbox`` 에 ``bsvibe.mcp.*`` 이 **한 건도 없었다**(5,571행 전부
execution/ontology — 아웃박스 자체는 살아 있다는 양성 대조군).

원인이 두 겹이었다:

1. **배선 지점이 0개.** prod 의 유일한 ``ToolContext(`` 생성(``mcp/server.py``)이
   ``audit_outbox`` 를 안 넘긴다. 기본값이 ``None`` 이라 ``_safe_audit_emit`` 이
   첫 줄에서 리턴한다 — 예외도, 경고도 없다.
2. ⭐ **기대하던 인터페이스가 아예 존재하지 않았다.** ``AuditOutboxLike`` 는
   ``is_open: bool`` 을 요구하는데, 그 이름은 코드 전체에서 **선언과 검사 두 곳에만**
   있다. REST 가 실제로 쓰는 것은 ``AuditEmitter.emit(event, session=...)`` 이고
   거기엔 ``is_open`` 이 없다. 즉 MCP 감사는 **상상 속 인터페이스**를 향해 설계됐고,
   그래서 배선하려 해도 붙일 것이 없었다.

⇒ 고치는 방향은 "없는 프로토콜에 구현을 만든다"가 아니라 **REST 와 같은 길을 쓴다**이다.

이 파일은 **행이 쌓이는 것**을 단언한다. 구조(무엇을 부르는가)가 아니라 결과다 —
구조만 보면 다음 리팩터가 같은 구멍을 다시 낼 수 있다.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select

import backend.connectors.db  # noqa: F401
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
from backend.config import get_settings
from backend.mcp.api import McpPrincipal, ToolContext
from backend.mcp.server import build_registry

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator:
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


def _principal(workspace_id: uuid.UUID) -> McpPrincipal:
    return McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        client_id="audit-test",
        scopes=frozenset(("mcp:read", "mcp:write")),
        jti=uuid.uuid4(),
    )


async def _outbox_count(session: Any) -> int:
    from plugin.audit.models import AuditOutboxRecord

    return int(
        (await session.execute(select(func.count()).select_from(AuditOutboxRecord))).scalar_one()
    )


def _tools_declaring_an_audit_event() -> list[Any]:
    """감사를 선언한 툴 전수 — **레지스트리에서** 센다.

    하드코딩 목록은 내가 아는 툴만 증명한다. 50번째 툴이 ``audit_event`` 를 달고
    들어와도 이 집합에 들어와야 한다.
    """
    registry = build_registry()
    return [
        t
        for name in registry.names()
        if (t := registry.get(name)) is not None and t.audit_event is not None
    ]


def test_the_declaring_set_is_large_and_not_empty() -> None:
    """크기 단언 — 집합이 비면 아래 전부가 공허하게 통과한다."""
    tools = _tools_declaring_an_audit_event()
    assert len(tools) >= 40, f"감사 선언 툴이 {len(tools)}개뿐이다 — 집합이 줄었다"
    names = {t.name for t in tools}
    # 되돌리기 어려운 쓰기 몇 개는 이름으로 못 박는다. 이들이 빠지면 사고다.
    for critical in (
        "bsvibe_connectors_delete",
        "bsvibe_model_accounts_delete",
        "bsvibe_bindings_create",
        "bsvibe_products_create",
    ):
        assert critical in names, f"{critical} 이 감사를 선언하지 않는다"


async def test_a_write_tool_leaves_an_audit_row(db) -> None:
    """⭐ 이 파일의 요점 — 디스패처를 통과하면 **행이 남는다.**

    prod 에서 0건이었던 그 행이다.
    """
    ws = uuid.uuid4()
    async with db() as session:
        before = await _outbox_count(session)

    registry = build_registry()
    async with db() as session:
        ctx = ToolContext(principal=_principal(ws), session=session)
        # 실패해도 좋다 — 감사는 핸들러 성공 뒤에 찍히므로, 성공하는 가장 싼 툴을 쓴다.
        await registry.call_tool("bsvibe_intents_create", {"name": "감사배선-테스트"}, ctx)
        # ⚠️ 여기서 커밋하지 **않는다.** prod 의 디스패처는 핸들러가 스스로 커밋한
        # 뒤 감사를 찍고 **아무도 다시 커밋하지 않는다** — 테스트가 커밋을 보태면
        # 그 구멍이 덮인다. 2026-09-23 에 실제로 그렇게 초록이 났고, prod 에서는
        # 여전히 0건이었다. 감사 경로가 **스스로** 커밋해야 한다.

    async with db() as session:
        after = await _outbox_count(session)
    assert after > before, "MCP 쓰기가 감사 행을 남기지 않았다"


async def test_the_audited_row_names_the_tool(db) -> None:
    """행이 남는 것만으로는 부족하다 — **무엇이 일어났는지**를 말해야 한다."""
    from plugin.audit.models import AuditOutboxRecord

    ws = uuid.uuid4()
    registry = build_registry()
    async with db() as session:
        ctx = ToolContext(principal=_principal(ws), session=session)
        await registry.call_tool("bsvibe_intents_create", {"name": "감사배선-테스트"}, ctx)
        # ⚠️ 여기서 커밋하지 **않는다.** prod 의 디스패처는 핸들러가 스스로 커밋한
        # 뒤 감사를 찍고 **아무도 다시 커밋하지 않는다** — 테스트가 커밋을 보태면
        # 그 구멍이 덮인다. 2026-09-23 에 실제로 그렇게 초록이 났고, prod 에서는
        # 여전히 0건이었다. 감사 경로가 **스스로** 커밋해야 한다.

    async with db() as session:
        rows = (
            (await session.execute(select(AuditOutboxRecord).order_by(AuditOutboxRecord.id.desc())))
            .scalars()
            .all()
        )
    assert rows, "행이 없다"
    latest = rows[0]
    assert latest.event_type.startswith("bsvibe.mcp."), latest.event_type
    assert "intents_create" in latest.event_type, latest.event_type


async def test_a_read_tool_leaves_no_row(db) -> None:
    """음성 대조군 — 전부 찍는 구현은 통과하면 안 된다.

    읽기 툴은 ``audit_event`` 를 선언하지 않는다. 그것까지 찍히면 아웃박스가
    잡음으로 차고, 그러면 아무도 안 본다.
    """
    ws = uuid.uuid4()
    registry = build_registry()
    async with db() as session:
        before = await _outbox_count(session)
        ctx = ToolContext(principal=_principal(ws), session=session)
        await registry.call_tool("bsvibe_intents_list", {}, ctx)
        after = await _outbox_count(session)
    assert after == before, "읽기 툴이 감사 행을 남겼다"


def test_the_emit_path_does_not_depend_on_a_field_nothing_sets() -> None:
    """⭐ 이 결함을 **조용하게** 만든 것.

    ``AuditOutboxLike`` 는 ``is_open: bool`` 을 요구했는데, 그 이름은 코드 전체에서
    선언과 검사 **두 곳에만** 있었다 — 어떤 구현도 세우지 않는다. 그 게이트가
    "감사가 꺼짐"과 "정상"을 표면에서 똑같이 보이게 했다.
    """
    import inspect
    import re

    from backend.mcp import api

    source = inspect.getsource(api._safe_audit_emit)
    # 독스트링은 **왜** 그 게이트가 사라졌는지를 적고 있고 그건 남아야 한다.
    # 명제는 "코드가 그 필드를 읽지 않는다" 이므로 독스트링을 걷어내고 본다.
    # (오늘 이 함정에 네 번 걸렸다 — 가드가 자기 설명에 걸리면 사람이 가드를 푼다.)
    code = re.sub(r'"""(?:.|\n)*?"""', "", source)
    assert "is_open" not in code, "아무도 세우지 않는 필드에 다시 기대고 있다"
    # 대조군 — 독스트링 제거가 함수를 통째로 삼키지 않았다
    assert "AuditEmitter" in code, "스트리퍼가 코드를 먹었다"
