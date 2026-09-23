"""텔레그램 계정의 ``chat_id`` 를 승인 허용목록으로 옮겨 심는다.

#1046 — 승인 권한 판정이 슬랙·디스코드와 통일되면서 텔레그램도
``delivery_config["authorized_user_ids"]`` 를 본다. 그 규칙은 **fail-closed** 라,
허용목록이 없는 계정은 **아무도 승인할 수 없다**.

⇒ 이 마이그레이션이 없으면 배포 순간 형님의 승인 버튼이 멈춘다.

1:1 채팅에서는 ``chat_id`` 가 **곧 그 사람의 user id** 다(그래서 옛 판정이
``from_id == chat_id`` 로 성립했다). 그 값을 허용목록에 담으면 형님 입장에서
**동작 변화 0** 이고, 새로 열리는 것은 그룹방뿐이다.

이미 허용목록이 있는 계정은 건드리지 않는다 — 사람이 넣은 값을 덮으면 안 된다.
``delivery_config`` 컬럼이 ``json`` 이라 ``jsonb`` 로 캐스팅해 연산한 뒤 되돌린다.

downgrade 는 심은 키를 지운다. ⚠️ 그 뒤에 사람이 **추가한** id 도 같이 사라진다 —
되돌릴 때 옛 코드는 ``chat_id`` 만 보므로 동작상 손실은 없지만, 목록 자체는 복구되지 않는다.
"""

from __future__ import annotations

from alembic import op

revision = "telegram_approval_allowlist"
down_revision = "trigger_default_no_dead_key"
branch_labels = None
depends_on = None

_SEED = """
UPDATE connector_accounts
   SET delivery_config = (
           (delivery_config::jsonb)
           || jsonb_build_object(
                  'authorized_user_ids',
                  jsonb_build_array(delivery_config::jsonb -> 'chat_id')
              )
       )::json
 WHERE connector = 'telegram'
   AND (delivery_config::jsonb) ? 'chat_id'
   AND NOT ((delivery_config::jsonb) ? 'authorized_user_ids')
"""

_UNSEED = """
UPDATE connector_accounts
   SET delivery_config = ((delivery_config::jsonb) - 'authorized_user_ids')::json
 WHERE connector = 'telegram'
   AND (delivery_config::jsonb) ? 'authorized_user_ids'
"""


def upgrade() -> None:
    op.execute(_SEED)


def downgrade() -> None:
    op.execute(_UNSEED)
