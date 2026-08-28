"""강제된 적 없는 워크스페이스별 ``region`` 축을 지운다.

#844 가 **읽는 쪽**을 정의 하나로 모았다. 이 마이그레이션은 **축 자체**를 지운다.

이 컬럼이 하는 일은 vault 경로의 가운데 세그먼트 하나였다 —
``<vault_root>/<region>/<workspace_id>/``. 라우팅도, 샤딩도, 데이터 레지던시 강제도
없었다. 그런데 답이 **두 개**였다: 쓰는 쪽(settle 워커·MCP 툴·product bootstrap·
anchor 백필 CLI)은 이 컬럼을, REST knowledge 라우트 전부는
``settings.knowledge_default_region`` 을 읽었다. prod 가 단일 리전이고 그 값이 곧
기본값이라(3/3 워크스페이스 ``us-1``) 아무도 눈치채지 못했다.

보이지 않았을 뿐 도달 가능했다 — API 가 create 와 PATCH 양쪽에서 임의의 ``region``
을 받았다. 기본값에서 한 필드만 어긋난 워크스페이스는 settle 훅이 A 에 쓰는 동안
REST 표면 전체가 B(빈 디렉터리)를 읽었을 것이다.

가운데 세그먼트는 ``settings.knowledge_default_region`` 배포 상수로 **남는다**.
지우는 것은 *"워크스페이스마다 다를 수 있다"* 는 주장이지 디렉터리 이름이 아니다.

이 저장소는 같은 모양의 축을 이미 폐기했다 —
``20260824_drop_data_jurisdiction.py``: *"강제된 적 없는 축을 지운다"*.

prod 실측 (2026-08-28): 3행 전부 ``us-1``. 값의 손실이 없다.

``downgrade`` 는 ``20260527_bundle_h_workspaces`` 의 ``upgrade`` 를 그대로 미러한다 —
NOT NULL 이므로 복원 시 서버 기본값 ``'us-1'`` 로 채운다. 원본이 server_default 를
남겨두므로 여기서도 떼지 않는다.

Revision ID: drop_workspace_region
Revises: note_embedding_content_hash
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "drop_workspace_region"
down_revision = "note_embedding_content_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("workspaces", "region")


def downgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("region", sa.String(length=32), nullable=False, server_default="us-1"),
    )
