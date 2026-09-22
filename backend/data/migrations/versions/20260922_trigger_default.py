"""resource_bindings.trigger 의 DDL 기본값에서 죽은 키를 걷어낸다.

#924 는 ``trigger.enabled`` 를 **파이썬 쪽에서만** 지웠다 — ORM 기본값과
``_default_trigger()``. DB 컬럼의 ``server_default`` 는 원래 모양
``'{"enabled": false, "filters": {}}'`` 그대로 남았고, e2e 체크리스트가 그걸
*"아무도 안 읽으므로 무해하지만 기록해 둔다"* 로 적어 뒀다.

무해하지 않다. **아래 층이 위 층 대신 대답한다** — 컬럼을 빼고 INSERT 하는 경로가
하나만 생기면 Postgres 가 죽은 키를 다시 써 넣고, 그건 위에서 무엇을 지웠든
상관없이 일어난다. 지운 키의 기본값을 DB 가 계속 들고 있는 한 삭제는 안 끝났다.

기존 3행도 같이 벗긴다. 읽는 코드가 **0개**임은
``tests/identity/test_trigger_knob_has_no_enabled.py`` 가 매 런 단언하므로
동작상 no-op 이고, 남겨 두면 API 응답과 PWA 가 계속 죽은 키를 실어 나른다.

downgrade 는 **기본값만** 되돌린다. 행에 죽은 키를 도로 심는 것은 복원이 아니라
재오염이다 — 그리고 그 값을 읽는 코드가 없으므로 되돌릴 동작도 없다.
"""

from __future__ import annotations

from alembic import op

revision = "trigger_default_no_dead_key"
down_revision = "task_claim_receipt"
branch_labels = None
depends_on = None

_LIVE = """'{"filters": {}}'"""
_DEAD = """'{"enabled": false, "filters": {}}'"""


def upgrade() -> None:
    op.execute(f"ALTER TABLE resource_bindings ALTER COLUMN trigger SET DEFAULT {_LIVE}::jsonb")
    op.execute(
        "UPDATE resource_bindings SET trigger = trigger - 'enabled' WHERE trigger ? 'enabled'"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE resource_bindings ALTER COLUMN trigger SET DEFAULT {_DEAD}::jsonb")
