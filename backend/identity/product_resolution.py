"""L-P1 — 형님의 direct 제출을 어느 제품에 묶을지 결정하는 단 하나의 규칙.

이 규칙은 원래 ``api/v1/messages.py`` 와 ``mcp/tools/direct_tools.py`` **양쪽**에
있었다. MCP 쪽이 *"Mirror L-P1 product-resolution logic from the REST messages
endpoint"* 라고 적어뒀지만 **미러가 아니었다** — MCP 는 슬러그로도 제품을 지목할 수
있었고 REST 는 UUID 만 받았다. 같은 규칙을 두 번 적었더니 한쪽만 능력을 얻었다.

**규칙은 공유하고 오류 표면은 남긴다.** 여기서는 못 찾으면 ``None`` 을 돌려주고,
부르는 쪽이 자기 프로토콜의 오류(HTTP 400 / ``ToolError``)를 던진다 — 그 둘은
합치면 안 되는 별개 축이다.

``ProductRow`` 가 이 컨텍스트에 살고, MCP 계약이 Identity 를 **명시적으로 허용**하므로
계약 예외 없이 양쪽이 의존한다.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.workspaces_db import ProductRow

__all__ = ["resolve_product_for_workspace"]


async def resolve_product_for_workspace(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    slug_or_id: str | None,
) -> uuid.UUID | None:
    """L-P1 우선순위대로 제품을 고른다.

    1. ``slug_or_id`` 가 이 워크스페이스의 제품을 가리키면 그것 (UUID 또는 슬러그).
    2. 아니면 이 워크스페이스에서 **가장 먼저 만들어진** 제품 — 단일 제품
       워크스페이스는 형님을 귀찮게 하지 않고, 다중 제품은 호출마다 덮어쓸 수 있다.
    3. 제품이 하나도 없으면 ``None``. 부르는 쪽이 자기 오류를 던진다 — 여기서
       조용히 NULL run 을 만들지 않는다.

    다른 워크스페이스의 제품을 지목하면 **조용히 기본값으로 떨어진다** (존재 여부를
    누설하지 않는다).
    """
    if slug_or_id:
        try:
            pid: uuid.UUID | None = uuid.UUID(slug_or_id)
        except ValueError:
            pid = None
        if pid is not None:
            row = await session.get(ProductRow, pid)
            if row is not None and row.workspace_id == workspace_id:
                return row.id
        by_slug = (
            await session.execute(
                select(ProductRow.id).where(
                    ProductRow.workspace_id == workspace_id,
                    ProductRow.slug == slug_or_id,
                )
            )
        ).scalar_one_or_none()
        if by_slug is not None:
            return by_slug

    return (
        await session.execute(
            select(ProductRow.id)
            .where(ProductRow.workspace_id == workspace_id)
            .order_by(ProductRow.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
