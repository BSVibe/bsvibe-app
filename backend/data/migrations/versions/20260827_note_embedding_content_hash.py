"""벡터가 **어떤 텍스트로** 만들어졌는지 기억하게 한다.

`reconcile_embeddings` 는 "이 경로에 벡터가 있나"만 물었다. 그래서 **틀린 텍스트로
만들어진 벡터**가 올바른 것과 구별되지 않았고, 영원히 건너뛰어졌다.

prod 실측 (2026-08-26, ollama/bge-m3, 실제 저장된 벡터):

    양성 대조군(같은 텍스트 재임베딩)  : 1.0000
    저장 벡터 vs settle `summary`      : 1.0000   ← 작업로그 한 줄에 붙어 있었다
    저장 벡터 vs 노트 본문             : 0.7006

#837 이 쓰기 지점을 고쳤지만 기존 행은 그대로였다. 고칠 수 있는 유일한 경로
(reconcile)가 그 행들을 구조적으로 안 봤기 때문이다.

이 컬럼이 그 구분을 준다. **NULL = 어떤 텍스트인지 모름**이고, 모름은 옳음의 증거가
아니므로 reconcile 이 재임베딩한다 — 즉 **백필이 곧 새 규칙**이지 일회성 스크립트가
아니다. 같은 드리프트가 다시 쌓여도 스스로 메워진다.

nullable 로 추가한다: 기존 1,724행은 NULL 로 남아 첫 reconcile 통과에서 교정된다.
``downgrade`` 는 컬럼만 되돌린다(벡터는 그대로 — 되돌려도 옛 동작인 "무조건 건너뜀"
으로 돌아갈 뿐 데이터 손실은 없다).

Revision ID: note_embedding_content_hash
Revises: drop_data_jurisdiction
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "note_embedding_content_hash"
down_revision = "drop_data_jurisdiction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "note_embeddings",
        sa.Column("content_hash", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("note_embeddings", "content_hash")
