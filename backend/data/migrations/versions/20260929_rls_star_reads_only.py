"""RLS 의 ``'*'`` 를 읽기에만 — ``DELETE`` 는 닫는다 (#959).

``rls_cross_tenant_read`` 는 ``'*'`` 를 ``USING`` 에 더하고 ``WITH CHECK`` 는 그대로 두어
"읽기만 열고 쓰기는 닫는다"고 했다. 그런데 ``DELETE`` 에는 새 행이 없어 ``WITH CHECK``
가 평가되지 않는다 — ``USING`` 만 본다. 명령 구분 없는(``FOR ALL``) 정책 하나였으니
``'*'`` 아래의 ``DELETE`` 가 모든 테넌트의 행을 지울 수 있었다(2026-09-29 실측 ``DELETE 1``).

그래서 명령별로 나눈다:

* ``SELECT`` — 자기 테넌트 또는 ``'*'``
* ``UPDATE`` — ``USING`` 은 ``'*'`` 를 포함한다: 큐 클레임의 ``SELECT … FOR UPDATE`` 는
  UPDATE 정책의 ``USING`` 도 통과해야 하고, 클레임은 ``'*'`` 로 후보를 잠근다(#1071).
  새 행은 ``WITH CHECK`` 가 자기 테넌트만 받는다
* ``INSERT`` / ``DELETE`` — 자기 테넌트만

빈 GUC 의 fail-open 은 그대로다 — 그걸 닫는 건 #959 ③.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "rls_star_reads_only"
down_revision = "rls_cross_tenant_read"
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
_GUC = "current_setting('app.current_workspace_id', true)"
_SINGLE = "rls_workspace_isolation"
_PER_COMMAND = ("rls_ws_select", "rls_ws_update", "rls_ws_insert", "rls_ws_delete")


def _own(col: str) -> str:
    return f"{_GUC} IS NULL OR {_GUC} = '' OR {col}::text = {_GUC}"


def _own_or_star(col: str) -> str:
    return f"{_own(col)} OR {_GUC} = '*'"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TABLES:
        col = _WS_COLUMN.get(table, "workspace_id")
        op.execute(sa.text(f"DROP POLICY IF EXISTS {_SINGLE} ON {table}"))
        for stmt in (
            f"CREATE POLICY rls_ws_select ON {table} FOR SELECT USING ({_own_or_star(col)})",
            f"CREATE POLICY rls_ws_update ON {table} FOR UPDATE "
            f"USING ({_own_or_star(col)}) WITH CHECK ({_own(col)})",
            f"CREATE POLICY rls_ws_insert ON {table} FOR INSERT WITH CHECK ({_own(col)})",
            f"CREATE POLICY rls_ws_delete ON {table} FOR DELETE USING ({_own(col)})",
        ):
            op.execute(sa.text(stmt))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TABLES:
        col = _WS_COLUMN.get(table, "workspace_id")
        for name in _PER_COMMAND:
            op.execute(sa.text(f"DROP POLICY IF EXISTS {name} ON {table}"))
        op.execute(
            sa.text(
                f"CREATE POLICY {_SINGLE} ON {table} "
                f"USING ({_own_or_star(col)}) WITH CHECK ({_own(col)})"
            )
        )
