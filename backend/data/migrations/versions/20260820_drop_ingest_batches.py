"""``ingest_batches`` 를 지운다 — producer 가 붙은 적 없는 두 번째 표현.

prod 실측 (2026-08-20): 행 **0**, ``IngestBatchRecorder`` 프로덕션 구현체 **0**,
``batch_recorder=`` 를 넘기는 생성 지점 **0**. 테이블은 있었고, Protocol 도 있었고,
컴파일러는 인자를 받았는데, 그 셋을 이어붙이는 코드가 한 번도 존재한 적 없다.

표류가 그 사실을 따로 증명한다: 이 테이블의 컬럼(``seed_count`` / ``decisions`` /
``model_used``)은 그것을 채웠어야 할 ``IngestBatchRecord`` 의 필드(``seed_source`` /
``notes_created`` / ``llm_calls`` / …)와 맞지도 않았다. 한 번도 함께 돈 적이 없다.

관측을 잃지 않는다 — 컴파일이 몇 번 돌았고 몇 개를 update 했는지는 워커 로그의
``ingest_compile_batch_complete`` 가 계속 남긴다(로컬 변수로만 만들어져 이 seam 을
거치지 않는다).

행이 0 이므로 데이터 손실이 없다. ``downgrade`` 는 스키마를 되돌리지만, 되돌린
테이블 역시 아무도 쓰지 않는다.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "drop_ingest_batches"
down_revision: Union[str, Sequence[str], None] = "one_pr_one_watch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "ingest_batches"
_INDEX = "ix_ingest_batches_ws_created"


def upgrade() -> None:
    op.drop_table(_TABLE)


def downgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("seed_count", sa.Integer(), nullable=False),
        sa.Column("decisions", sa.JSON(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("model_used", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(_INDEX, _TABLE, ["workspace_id", "created_at"])
