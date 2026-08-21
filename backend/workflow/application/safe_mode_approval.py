"""Safe-Mode 승인의 **오케스트레이션** — 세 표면이 공유하는 단 하나의 자리.

``SafeModeQueue.approve`` 는 이미 공유 원시연산이었다. 문제는 그 **주변**이었다 —
"pending 을 찾고 · 승인하고 · 커밋하고 · artifact_type 을 정하고 · 배달하고 ·
보상 핸들을 남긴다" 는 여섯 단계가 REST · MCP · 커넥터 콜백 **세 벌**로 적혀 있었다.

그 중복은 이미 두 번 실제 결함을 냈다:

* **Lift E40** — MCP 승인이 dispatch 를 아예 안 했다. 큐 행만 뒤집고 "워커가 다음
  틱에" 를 믿었는데 **워커는 ``delivery_events`` 를 드레인하지 safe_mode 큐를 보지
  않는다.** 런은 ``review_ready`` 에 닿았고 PR 은 열리지 않았다 (run 1079bff5).
* **#771** — MCP 승인만 **보상 핸들을 버렸다.** 그 딜리버러블은 영원히 retract
  불가였는데 호출은 ``dispatched=True`` 를 돌려줬다.

그리고 통합 시점에 **두 갈래가 더** 살아 있었다:

* REST 는 dispatch 를 감싸지 않아, **승인이 이미 커밋된 뒤** 터지면 HTTP 500 이
  나갔다. 형님이 재시도하면 그 항목은 더 이상 pending 이 아니라 404 다 — 승인은
  됐고, 배달은 안 됐고, 그 사실을 알려주는 신호는 없었다.
* ``artifact_type`` 해석이 세 곳 다 달랐다.

⚠️ **승인은 되돌리지 않는다.** dispatch 실패는 best-effort 로 삼키고
``dispatched=False`` 로 정직하게 말한다 — 세 표면이 각자 지키던 불변식이다.

⚠️ **오류 표면은 여기서 정하지 않는다.** :class:`ApproveOutcome` 을 돌려주고,
HTTP 404/409 · ``ToolError`` · 콜백의 조용한 반환은 각 프로토콜이 옮긴다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.application.safe_mode_queue import SafeModeQueue

logger = structlog.get_logger(__name__)

__all__ = [
    "ApproveOutcome",
    "approve_and_dispatch",
    "approve_run_and_dispatch",
    "artifact_type_for_deliverable",
]


@dataclass(frozen=True)
class ApproveOutcome:
    """승인 시도의 결과 — 부르는 쪽이 자기 프로토콜의 응답으로 옮긴다.

    ``found=False``  → 그 워크스페이스에 pending 항목이 없다 (REST 404).
    ``approved=False`` (found=True) → 경합에서 졌다, 이미 pending 이 아니다 (REST 409).
    ``dispatched``   → 배달이 실제로 나갔나. **승인 성공과 별개 축이다.**
    """

    found: bool
    approved: bool
    dispatched: bool
    deliverable_id: uuid.UUID | None


async def artifact_type_for_deliverable(session: AsyncSession, deliverable_id: uuid.UUID) -> str:
    """딜리버러블의 artifact type — 세 표면이 제각각 풀던 것을 한 곳으로.

    행이 없으면 ``"direct_output"`` — 커넥터 콜백이 쓰던 폴백이 가장 관대해서
    그것을 택했다 (없는 행 때문에 배달을 막지 않는다).
    """
    from backend.workflow.infrastructure.db import Deliverable  # noqa: PLC0415

    row = await session.get(Deliverable, deliverable_id)
    if row is None:
        return "direct_output"
    kind = row.deliverable_type
    return kind.value if hasattr(kind, "value") else str(kind)


async def approve_and_dispatch(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    item_id: uuid.UUID,
    actor_id: uuid.UUID,
    dispatcher: Any | None,
) -> ApproveOutcome:
    """pending 항목을 승인하고 배달까지 내보낸다.

    ``dispatcher`` 가 ``None`` 이면 승인만 하고 ``dispatched=False`` 를 돌려준다 —
    배달 어댑터가 배선되지 않은 배포에서도 승인 자체는 기록돼야 한다.
    """
    queue = SafeModeQueue(session)
    pending = {item.id: item for item in await queue.list_pending(workspace_id=workspace_id)}
    item = pending.get(item_id)
    if item is None:
        return ApproveOutcome(found=False, approved=False, dispatched=False, deliverable_id=None)

    deliverable_id = item.deliverable_id
    approved = await queue.approve(workspace_id=workspace_id, item_id=item_id, actor_id=actor_id)
    await session.commit()
    if not approved:  # 경합에서 졌다 — 다시 읽으니 더 이상 pending 이 아니다
        return ApproveOutcome(
            found=True, approved=False, dispatched=False, deliverable_id=deliverable_id
        )

    dispatched = await _dispatch_best_effort(
        session, workspace_id=workspace_id, deliverable_id=deliverable_id, dispatcher=dispatcher
    )
    return ApproveOutcome(
        found=True, approved=True, dispatched=dispatched, deliverable_id=deliverable_id
    )


async def _dispatch_best_effort(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    dispatcher: Any | None,
) -> bool:
    """배달 + 보상 핸들 저장. **실패해도 승인을 되돌리지 않는다.**

    보상 핸들이 여기 있는 이유가 #771 이다 — 표면마다 저장하면 언젠가 하나가
    빠뜨리고, 그러면 그 딜리버러블만 조용히 retract 불가가 된다.
    """
    if dispatcher is None:
        logger.warning(
            "safe_mode_approve_no_dispatcher_configured", deliverable_id=str(deliverable_id)
        )
        return False

    # 지연 import — ``connectors.approval_callback`` 이 이 모듈을 타고 들어와도
    # ``backend.api.webhooks`` 가 ``plugin.*`` 정적 엣지를 얻지 않게 한다 (R2c).
    from backend.workflow.infrastructure.workers.delivery_worker import (  # noqa: PLC0415
        dispatch_delivery,
        persist_compensation_handles,
    )

    artifact_type = await artifact_type_for_deliverable(session, deliverable_id)
    try:
        result = await dispatch_delivery(
            dispatcher,
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,
            # 런 자동 해소 seam — 배달 성공 시 이 런의 대기 중 review Decision 을
            # 해소하고 런을 ship 한다. 호출자의 트랜잭션 안에서.
            session=session,
        )
    except Exception:  # noqa: BLE001 — 승인은 이미 커밋됐다. 배달은 best-effort 다.
        logger.warning(
            "safe_mode_approve_dispatch_failed",
            deliverable_id=str(deliverable_id),
            exc_info=True,
        )
        return False

    await persist_compensation_handles(session, deliverable_id=deliverable_id, result=result)
    return True


async def approve_run_and_dispatch(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    actor_id: uuid.UUID,
    dispatcher: Any | None,
) -> tuple[int, int]:
    """런의 대기 중 항목을 **전부** 승인하고 배달한다 → ``(approved, dispatched)``.

    항목 하나의 배달 실패가 다른 항목을 막지 않고, **어떤 승인도 되돌리지 않는다** —
    단건 경로와 같은 불변식이다. 대기 항목이 없으면 ``(0, 0)``.

    승인은 **먼저 전부** 뒤집고 한 번에 커밋한다. 배달 루프는 그 뒤다 — 배달 도중
    터져도 승인 상태는 이미 확정돼 있다.
    """
    queue = SafeModeQueue(session)
    pending = await queue.list_pending_for_run(workspace_id=workspace_id, run_id=run_id)
    if not pending:
        return (0, 0)

    # 승인이 ``decided_at`` 을 세팅하므로 대상 목록을 미리 스냅샷한다 —
    # 배달 루프가 큐 행 상태에 의존하지 않게.
    targets = [(item.id, item.deliverable_id) for item in pending]
    approved_ids = {
        item_id
        for item_id, _ in targets
        if await queue.approve(workspace_id=workspace_id, item_id=item_id, actor_id=actor_id)
    }
    await session.commit()

    dispatched = 0
    for item_id, deliverable_id in targets:
        if item_id not in approved_ids:
            continue
        if await _dispatch_best_effort(
            session,
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            dispatcher=dispatcher,
        ):
            dispatched += 1
    return (len(approved_ids), dispatched)
