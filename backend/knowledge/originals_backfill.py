"""이미 쌓인 원본을 vault 로 내린다 — 한 번 돌리고 끝나는 백필.

배선(#854)은 **앞으로** 발생하는 것만 남긴다. 형님이 말한 *"원본 지식은
히스토리성이기 때문에 보존된다"* 는 과거가 있어야 성립한다 — prod 실측
(2026-08-31): 요청 223건 · settle 147건이 DB 에만 있고 vault 에는 0건이었다.

## 파생 규칙을 다시 적지 않는다

payload → 원본 변환은 :func:`~backend.knowledge.infrastructure.workers.settle_worker.settlement_originals`
**하나뿐**이고 이 모듈은 그것을 호출한다. 규칙이 두 벌이 되는 순간 §13 원클릭
가드가 한쪽에서만 지켜지고, 형님이 한 글자도 안 쓴 '피드백'이 vault 에 쌓인다
(prod 2026-08-25 실측: ``decision_resolution`` 11건 중 6건이 그랬다).

## 왜 상한 + ``remaining`` 인가

HTTP/MCP 표면은 오래 걸리는 작업을 끝까지 못 들고 있다 — prod 2026-08-28 에
524 가 **성공한 작업을 실패로 배달**했다. 이 백필은 멱등이므로(``record_original``
이 ``O_EXCL``) 한 번에 다 할 필요가 없다: 상한을 두고 ``remaining`` 을 돌려주면
호출자가 다시 부르면 된다. 잡 테이블도, 진행률 행도 필요 없다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from backend.knowledge.infrastructure.workers.settle_worker import (
    SETTLE_ACTIVITY_TYPE,
    settlement_originals,
    to_settlement,
)
from backend.workflow.infrastructure.db import ExecutionRunActivity
from backend.workflow.infrastructure.intake.db import RequestRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.knowledge.originals import OriginalKind

logger = structlog.get_logger(__name__)

#: 한 패스가 **새로 쓰는** 원본 수의 상한.
#:
#: ⚠️ 읽기가 아니라 **쓰기**에 건다. 읽기에 걸면 매 패스가 같은 앞부분을 다시
#: 훑고, 이미 기록된 행이 예산을 다 먹어 그 뒤로는 영원히 도달하지 못한다 —
#: ``remaining`` 이 줄지 않는 채로 "성공"이 반복된다. 쓰기에 걸면 이미 있는
#: 것은 예산을 안 쓰므로 패스마다 반드시 앞으로 나아간다.
DEFAULT_LIMIT = 200

#: 한 패스가 읽는 소스 행 수의 안전 상한 — 코퍼스가 폭주해도 메모리를 지킨다.
#: prod 전체가 요청 223 + settle 147 이라 실제로는 걸리지 않는다.
_MAX_SCAN = 5_000


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """한 패스의 결과.

    ``pending`` 은 **이번 패스가 쓸 수 있었던 원본 수**(dry-run 이면 쓰지 않고
    센 것), ``recorded`` 는 실제로 새로 쓴 수, ``already`` 는 이미 있어서 건너뛴
    수, ``remaining`` 은 상한에 걸려 이번에 못 본 **소스 행** 수다.
    """

    scanned: int
    pending: int
    recorded: int
    already: int
    remaining: int


async def backfill_originals(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    vault_root: Path,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
) -> BackfillResult:
    """``workspace_id`` 의 과거 요청·피드백·회고를 vault 로 내린다.

    한 워크스페이스만 본다 — 백필은 호출자의 경계를 넘지 않는다.
    """
    from backend.knowledge.factory import KnowledgeFactory  # noqa: PLC0415 — lazy heavy import
    from backend.knowledge.originals import record_original  # noqa: PLC0415

    candidates: list[tuple[OriginalKind, str, str, str, dict[str, str]]] = []
    scanned = 0

    request_rows = (
        (
            await session.execute(
                select(RequestRow)
                .where(RequestRow.workspace_id == workspace_id)
                .order_by(RequestRow.created_at.asc())
                .limit(_MAX_SCAN)
            )
        )
        .scalars()
        .all()
    )
    scanned += len(request_rows)
    for req in request_rows:
        payload = req.payload or {}
        raw = payload.get("text") or payload.get("intent_text")
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidates.append(
            (
                "request",
                str(req.id),
                raw.strip().splitlines()[0][:80],
                raw,
                {"request_id": str(req.id)},
            )
        )

    settle_rows = (
        (
            await session.execute(
                select(ExecutionRunActivity)
                .where(
                    ExecutionRunActivity.workspace_id == workspace_id,
                    ExecutionRunActivity.activity_type == SETTLE_ACTIVITY_TYPE,
                )
                .order_by(ExecutionRunActivity.created_at.asc())
                .limit(_MAX_SCAN)
            )
        )
        .scalars()
        .all()
    )
    scanned += len(settle_rows)
    for row in settle_rows:
        settlement = to_settlement(row)
        provenance = {"run_id": str(row.run_id), "activity_id": str(row.id)}
        for kind, key, title, content in settlement_originals(settlement):
            candidates.append((kind, key, title, content, provenance))

    if dry_run:
        return BackfillResult(
            scanned=scanned,
            pending=len(candidates),
            recorded=0,
            already=0,
            remaining=max(0, len(candidates) - limit),
        )

    vault = KnowledgeFactory(workspace_id=str(workspace_id), vault_root=vault_root).vault()
    recorded = 0
    already = 0
    remaining = 0
    for kind, key, title, content, provenance in candidates:
        if recorded >= limit:
            # 예산 소진. 남은 후보는 다음 패스의 몫 — 이미 있는 것은 예산을 쓰지
            # 않았으므로 다음 패스는 반드시 여기서부터 앞으로 나아간다.
            remaining += 1
            continue
        written = await record_original(
            vault=vault, kind=kind, key=key, title=title, content=content, provenance=provenance
        )
        if written is None:
            already += 1
        else:
            recorded += 1

    logger.info(
        "originals_backfilled",
        workspace_id=str(workspace_id),
        scanned=scanned,
        recorded=recorded,
        already=already,
        remaining=remaining,
    )
    return BackfillResult(
        scanned=scanned,
        pending=len(candidates),
        recorded=recorded,
        already=already,
        remaining=remaining,
    )


__all__ = ["DEFAULT_LIMIT", "BackfillResult", "backfill_originals"]
