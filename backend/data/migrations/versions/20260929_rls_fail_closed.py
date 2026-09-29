"""RLS 를 fail-closed 로 — 빈 GUC 탈출구를 닫는다 (#959 ③).

정책은 지금까지 ``current_setting IS NULL OR = ''`` 이면 모든 행을 통과시켰다
(fail-open). 워크스페이스를 publish 하지 않은 코드가 **모든 테넌트**를 보고
아무 테넌트에나 쓸 수 있었다는 뜻이다.

닫는 전제는 측정으로 채웠다(2026-09-28 ~ 09-29):

* 배경 워커 · 웹훅 · 운영 CLI 의 블라인드 접근을 스코프나 명시적 교차테넌트 읽기
  (``'*'``)로 옮겼다(#1067 ~ #1071, #1076, #1090)
* 테스트가 prod 와 같은 정책으로 돈다 — 로컬 fail-closed 전체 스위트 983 → 0 실패
  (#1083 ~ #1089)

이제 빈 GUC 는 **0행**이고 쓰기는 거절된다. 남은 탈출구는 이름으로 청하는
``'*'`` 하나 — 읽기(SELECT · UPDATE 의 대상 선택)에만 열린다(``rls_star_reads_only``).

⚠️ 되돌리기: 컨테이너 entrypoint 가 부팅마다 ``alembic upgrade head`` 를 돈다.
``downgrade -1`` 만 하면 다음 재시작에 다시 닫힌다 — 이 마이그레이션을 담은
커밋을 revert 해야 main 에서 빠진다.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "rls_fail_closed"
down_revision = "rls_star_reads_only"
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


def _recreate(*, fail_open: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TABLES:
        col = _WS_COLUMN.get(table, "workspace_id")
        own = f"{col}::text = {_GUC}"
        if fail_open:
            own = f"{_GUC} IS NULL OR {_GUC} = '' OR {own}"
        own_or_star = f"{own} OR {_GUC} = '*'"
        for name in ("rls_ws_select", "rls_ws_update", "rls_ws_insert", "rls_ws_delete"):
            op.execute(sa.text(f"DROP POLICY IF EXISTS {name} ON {table}"))
        for stmt in (
            f"CREATE POLICY rls_ws_select ON {table} FOR SELECT USING ({own_or_star})",
            f"CREATE POLICY rls_ws_update ON {table} FOR UPDATE "
            f"USING ({own_or_star}) WITH CHECK ({own})",
            f"CREATE POLICY rls_ws_insert ON {table} FOR INSERT WITH CHECK ({own})",
            f"CREATE POLICY rls_ws_delete ON {table} FOR DELETE USING ({own})",
        ):
            op.execute(sa.text(stmt))


def upgrade() -> None:
    _recreate(fail_open=False)


def downgrade() -> None:
    _recreate(fail_open=True)
