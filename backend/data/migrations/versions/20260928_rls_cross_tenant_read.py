"""RLS 정책 ``USING`` 에만 명시적 교차테넌트 읽기(``'*'``)를 연다 (#959).

배경 워커는 테넌트를 가로질러 **읽어야** 한다 — 큐 클레임, 전역 스윕, 테넌트
열거. 2026-09-28 정책에 프로브를 붙여 재니 배경 워커 진입점의 블라인드 접근이
11곳이었고, fail-closed 정책에서 빈 GUC 는 에러가 아니라 **0행**이라 그 경로들이
소리 없이 멈춘다.

그래서 이름으로 요청해야만 열리는 탈출구를 둔다: GUC 값 ``'*'`` 는 ``USING``
(행 가시성)만 연다. ``WITH CHECK`` 는 그대로 행 자신의 워크스페이스를 요구하므로
모든 **쓰기**는 여전히 그 테넌트 스코프 안에서만 된다. 앱 쪽 입구는
:func:`backend.data.rls.cross_tenant_read` 하나다.

빈 GUC 의 fail-open 은 이 마이그레이션이 건드리지 않는다 — 그걸 닫는 건 11곳이
``'*'`` 로 옮겨간 다음 단계다.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "rls_cross_tenant_read"
down_revision = "telegram_approval_allowlist"
branch_labels = None
depends_on = None

_TABLES: tuple[str, ...] = (
    "workspaces",
    "products",
    "execution_runs",
    "deliverables",
    "execution_decisions",
    "requests",
)
_WS_COLUMN: dict[str, str] = {"workspaces": "id"}
_POLICY = "rls_workspace_isolation"
_GUC = "current_setting('app.current_workspace_id', true)"


def _own_tenant(col: str) -> str:
    return f"{_GUC} IS NULL OR {_GUC} = '' OR {col}::text = {_GUC}"


def _recreate(*, cross_tenant_read: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TABLES:
        col = _WS_COLUMN.get(table, "workspace_id")
        using = _own_tenant(col) + (f" OR {_GUC} = '*'" if cross_tenant_read else "")
        op.execute(sa.text(f"DROP POLICY IF EXISTS {_POLICY} ON {table}"))
        op.execute(
            sa.text(
                f"CREATE POLICY {_POLICY} ON {table} "
                f"USING ({using}) WITH CHECK ({_own_tenant(col)})"
            )
        )


def upgrade() -> None:
    _recreate(cross_tenant_read=True)


def downgrade() -> None:
    _recreate(cross_tenant_read=False)
