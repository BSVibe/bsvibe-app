"""SafeModeQueue — founder approval gate for outbound deliveries.

Workflow §12.5 #8 (Bundle G — Delivery) and Workflow §10.5 (Safe Mode).
When the workspace is in Safe Mode, every deliverable lands here instead
of auto-dispatching; the founder approves or denies via the queue UI, and
the dispatcher only runs on approval.

Retention window (Workflow §10.5):

* Initial active window: **90 days** from enqueue
* Per-item extension: **+30 days**, max **2** extensions (so 90 + 30 + 30 = 150 days max)
* After expiry: item flips to ``expired`` status (no further action)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.settle_kinds import NEGATIVE_PATTERN_SETTLE_KIND, founder_authored_text
from backend.workflow.domain.repositories import SafeModeQueueRepository
from backend.workflow.infrastructure.db import RunStatus
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus
from backend.workflow.infrastructure.repositories import SqlAlchemySafeModeQueueRepository

logger = structlog.get_logger(__name__)

INITIAL_TTL_DAYS = 90
EXTENSION_TTL_DAYS = 30
MAX_EXTENSIONS = 2

#: A run that already ENDED has nowhere to resume to — reopening it would re-run
#: finished work, including its approval + delivery. Kept local for the same
#: reason ``checkpoint_resolution`` keeps its own copy: the SET matches but the
#: reason differs, and a shared constant would couple two unrelated decisions.
_TERMINAL_RUN_STATUSES = frozenset({RunStatus.SHIPPED, RunStatus.FAILED, RunStatus.CANCELLED})


class SafeModeQueue:
    """Pull-based approval queue for outbound deliveries.

    Lifecycle service over :class:`SafeModeQueueItemRow`. Persistence is
    delegated to a :class:`SafeModeQueueRepository` (Lift I-Repo-Workflow-2);
    this class owns the transitions, retention math, and audit/log surface.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        repository: SafeModeQueueRepository | None = None,
    ) -> None:
        self._session = session
        # Default-constructed for backward compat with existing call sites
        # (``SafeModeQueue(session)``). Tests + new callers inject a Protocol.
        self._repo: SafeModeQueueRepository = repository or SqlAlchemySafeModeQueueRepository(
            session
        )

    async def enqueue(
        self,
        *,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Enqueue a pending delivery; returns the queue item id.

        ``run_id`` is the optional per-Run grouping key (B12a / Workflow §1.2).
        Existing callers omit it and pre-B12a rows keep working; new callers
        (DeliveryWorker) always thread the originating event's run_id through.
        """
        now = datetime.now(tz=UTC)
        row = SafeModeQueueItemRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            run_id=run_id,
            status=SafeModeStatus.PENDING,
            expires_at=now + timedelta(days=INITIAL_TTL_DAYS),
            extension_count=0,
            created_at=now,
        )
        await self._repo.enqueue(row, producer_id="worker:delivery_worker")
        await self._session.flush()
        logger.info(
            "safe_mode_enqueued",
            workspace_id=str(workspace_id),
            deliverable_id=str(deliverable_id),
            item_id=str(row.id),
        )
        return row.id

    async def list_pending(self, *, workspace_id: uuid.UUID) -> list[SafeModeQueueItemRow]:
        """Founder-facing list of items awaiting approval (newest first)."""
        return await self._repo.list_pending_by_workspace(workspace_id)

    async def list_pending_for_run(
        self, *, workspace_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[SafeModeQueueItemRow]:
        """The pending items for one run (B12a) — drives per-Run approve.

        Returned in creation order (oldest first) so dispatch happens in the
        same order the agent loop emitted the artifacts. Empty list when the
        run has no pending items (or never existed)."""
        return await self._repo.list_pending_for_run(workspace_id=workspace_id, run_id=run_id)

    async def list_resolved(self, *, workspace_id: uuid.UUID) -> list[SafeModeQueueItemRow]:
        """Founder-facing list of decided items (approved / denied / expired),
        most-recently-decided first. Powers the Decisions "Resolved" tab's
        delivery side; ``decided_at`` is the sort key (created_at as a stable
        tiebreaker for a defensively-undecided row)."""
        return await self._repo.list_resolved_by_workspace(workspace_id)

    async def approve(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> bool:
        """Flip ``pending → approved``. Returns False if not found / not pending.

        The caller is responsible for handing the deliverable to the
        :class:`backend.workflow.application.delivery.dispatcher.DeliveryDispatcher` AFTER the
        commit succeeds.
        """
        return await self._transition(
            workspace_id=workspace_id,
            item_id=item_id,
            from_status=SafeModeStatus.PENDING,
            to_status=SafeModeStatus.APPROVED,
            actor_id=actor_id,
        )

    async def deny(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
        actor_id: uuid.UUID,
        reason: str,
    ) -> bool:
        """Flip ``pending → denied``. Returns False if not found / not pending.

        The caller is responsible for any downstream notification — the
        deny is purely a state transition. (D3b's auto-compensation wiring
        was rolled back under Lift 0 / v8 §13 D7 as YAGNI; the surface that
        actually shipped — :class:`backend.delivery.compensation.CompensationHandler` —
        was never carried by a real workflow other than this one fan-out.)
        """
        # §13 — the SAME sentence the settle sink now enforces on every producer
        # (:func:`~backend.common.settle_kinds.founder_authored_text`): a
        # settlement is knowledge only when the founder actually wrote something.
        # This producer already held that precondition inline; routing it through
        # the shared leaf makes it the one rule rather than a second copy that can
        # drift — the drift is exactly what let ``resolve_checkpoint`` write 6
        # zero-text notes. Redundant with the sink gate on purpose (defense in
        # depth): it ALSO gates the run re-open below, which is not a knowledge
        # concern and must keep its own guard.
        reason_text = founder_authored_text(answer=None, reason=reason, action_key=None)
        flipped = await self._transition(
            workspace_id=workspace_id,
            item_id=item_id,
            from_status=SafeModeStatus.PENDING,
            to_status=SafeModeStatus.DENIED,
            actor_id=actor_id,
            # A blank reason teaches nothing — store NULL rather than "" so a
            # later reader cannot mistake emptiness for a recorded judgment.
            reason=reason_text,
        )
        if flipped and reason_text:
            await self._record_rejection_knowledge(
                workspace_id=workspace_id, item_id=item_id, actor_id=actor_id, reason=reason_text
            )
            await self._reopen_run_with_the_reason(item_id=item_id, reason=reason_text)
        return flipped

    async def _record_rejection_knowledge(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
        actor_id: uuid.UUID,
        reason: str,
    ) -> None:
        """A-1b — 거절을 재사용 가능한 negative knowledge 로 남긴다.

        ``checkpoint_resolution`` 의 discard 분기가 이미 쓰는 seam 을 그대로 재사용한다
        (``settle`` 활동 → SettleWorker → vault → ``NegativePatternRetriever``). 새
        파이프라인이 아니라 **끊긴 링크 하나**다: 그 분기는 checkpoint 경로에만 있었고
        실제 거절은 Safe Mode 로 일어난다 (prod 실측 2026-08-16: 거절 91건, 서로 다른
        런 67개, negative-pattern 행 0개).

        매칭 근거는 형님이 직접 쓴 텍스트뿐이다 — ``reason`` + 런의 ``intent_text``.
        딜리버러블 요약은 LLM 생성물이라 쓰지 않는다 (``NegativePatternRetriever`` 의
        *"never an LLM-generated body"* 규율).

        ``run_id`` 없는 항목은 클러스터링 컨텍스트가 없어 조용히 건너뛴다 — 거절 자체는
        이미 성공했고, 지식화 실패가 형님의 판단을 막아선 안 된다.
        """
        import uuid as _uuid  # noqa: PLC0415

        from backend.workflow.domain.verified_deliverable import (  # noqa: PLC0415
            settle_run_context,
        )
        from backend.workflow.infrastructure.db import (  # noqa: PLC0415
            ExecutionRun,
            ExecutionRunActivity,
        )

        row = await self._repo.get(item_id)
        if row is None or row.run_id is None:
            return
        run = await self._session.get(ExecutionRun, row.run_id)
        if run is None:
            return
        self._session.add(
            ExecutionRunActivity(
                id=_uuid.uuid4(),
                run_id=run.id,
                workspace_id=workspace_id,
                activity_type="settle",
                payload={
                    "kind": NEGATIVE_PATTERN_SETTLE_KIND,
                    "safe_mode_item_id": str(item_id),
                    "deliverable_id": str(row.deliverable_id),
                    "reason": reason,
                    "resolved_by": str(actor_id),
                    "resolved_at": datetime.now(tz=UTC).isoformat(),
                    # 거절은 정직한 형님 신호이지 검증된 코드가 아니다.
                    "verified": False,
                    "summary": f"Rejected approach — {reason}"[:2000],
                    **await settle_run_context(self._session, run),
                },
            )
        )
        await self._session.flush()

    async def _reopen_run_with_the_reason(self, *, item_id: uuid.UUID, reason: str) -> None:
        """A-1c — 거절을 **그 런에** 도달시킨다.

        A-1b 는 거절을 미래 런을 위한 지식으로 만들었다. 정작 거절당한 런은 아무것도
        못 듣는다 — Safe Mode 의 ``deny`` 가 런을 건드리지 않기 때문이다. 그 링크는
        Decision 경로에만 있었고(``checkpoint_resolution``: RUNNING → OPEN + 답변을
        맥락에 접어 넣음), **실제 거절은 전부 Safe Mode 로 일어난다.**

        새 서브시스템이 아니라 **링크 하나**다. 기계는 이미 다 있다:
        ``payload["resolved_decisions"]`` 를 ``_loop_context._resumption_messages`` 가
        읽어 user 메시지로 만들고, OPEN 런은 ``AgentWorker.drive_once`` 가 다시 집는다.

        접어 넣는 텍스트는 **형님이 직접 쓴 사유뿐**이다 — 딜리버러블 요약은 LLM
        생성물이라 쓰지 않는다 (A-1b 와 같은 규율).

        상태 hop 은 ``ExecutionRunHistory`` 직접 쓰기다. :class:`AgentRunner` 를
        import 하면 run-engine 그래프 전체가 커넥터의 인바운드 콜백 경로로 끌려와
        R2c(인바운드 레이어는 plugin-free) 를 깬다 — ``run_delivery_resolution`` 이
        같은 이유로 같은 패턴을 쓴다.
        """
        import uuid as _uuid  # noqa: PLC0415

        from backend.workflow.infrastructure.db import (  # noqa: PLC0415
            ExecutionRun,
            ExecutionRunHistory,
        )

        row = await self._repo.get(item_id)
        if row is None or row.run_id is None:
            return
        run = await self._session.get(ExecutionRun, row.run_id)
        if run is None or run.status in _TERMINAL_RUN_STATUSES:
            # 이미 끝난 런은 재개할 곳이 없다 — 되살리면 승인·배달까지 다시 돈다.
            return

        # JSON 컬럼은 **재할당**해야 SQLAlchemy 가 변경을 감지한다 (in-place 변경은
        # 조용히 유실된다 — ``checkpoint_resolution`` 이 같은 주의를 달아뒀다).
        payload: dict[str, Any] = dict(run.payload or {})
        resolved = list(payload.get("resolved_decisions") or [])
        resolved.append(
            {
                "decision_id": str(item_id),
                "question": "Approve delivering this run's result?",
                "answer": f"Denied. {reason}",
            }
        )
        payload["resolved_decisions"] = resolved
        run.payload = payload

        now = datetime.now(tz=UTC)
        from_status = run.status
        run.status = RunStatus.OPEN
        run.updated_at = now
        self._session.add(
            ExecutionRunHistory(
                id=_uuid.uuid4(),
                run_id=run.id,
                workspace_id=run.workspace_id,
                from_status=from_status,
                to_status=RunStatus.OPEN,
                reason=f"reopened: safe mode item {item_id} denied with a reason",
                created_at=now,
            )
        )
        await self._session.flush()

    async def mark_delivered(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
    ) -> bool:
        """Flip ``approved → delivered`` (D3 lifecycle).

        Records that an approved item's outbound dispatch actually succeeded.
        Returns False if not found / not in ``approved`` — the edge is enforced,
        so an un-approved (pending) item cannot be marked delivered.
        """
        return await self._transition(
            workspace_id=workspace_id,
            item_id=item_id,
            from_status=SafeModeStatus.APPROVED,
            to_status=SafeModeStatus.DELIVERED,
            stamp_decided=False,
        )

    async def archive(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
    ) -> bool:
        """Park a settled item out of the active queue → ``archived`` (D3).

        Allowed from any terminal-decision state (``delivered`` / ``denied`` /
        ``expired``). Returns False if not found or still pending/approved.
        """
        row = await self._repo.get(item_id)
        if row is None or row.workspace_id != workspace_id:
            return False
        if row.status not in (
            SafeModeStatus.DELIVERED,
            SafeModeStatus.DENIED,
            SafeModeStatus.EXPIRED,
        ):
            return False
        row.status = SafeModeStatus.ARCHIVED
        await self._session.flush()
        return True

    async def mark_deleted(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
    ) -> bool:
        """Soft-tombstone an archived item → ``deleted`` (D3 retention sweep).

        Returns False if not found / not ``archived``.
        """
        return await self._transition(
            workspace_id=workspace_id,
            item_id=item_id,
            from_status=SafeModeStatus.ARCHIVED,
            to_status=SafeModeStatus.DELETED,
            stamp_decided=False,
        )

    async def extend(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
    ) -> bool:
        """Extend the active window by ``EXTENSION_TTL_DAYS``.

        Returns False if not found OR already at ``MAX_EXTENSIONS``.
        """
        row = await self._repo.get(item_id)
        if row is None or row.workspace_id != workspace_id:
            return False
        if row.status not in (SafeModeStatus.PENDING, SafeModeStatus.EXTENDED):
            return False
        if row.extension_count >= MAX_EXTENSIONS:
            return False
        row.extension_count += 1
        row.expires_at = row.expires_at + timedelta(days=EXTENSION_TTL_DAYS)
        row.status = SafeModeStatus.EXTENDED
        await self._session.flush()
        return True

    async def mark_expired(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
    ) -> bool:
        """Flip ONE item ``pending → expired`` (or ``extended → expired``).

        Mirrors the per-item :meth:`mark_delivered` / :meth:`mark_deleted`
        vocabulary so the lifecycle is enum-shaped + glass-box (the
        :class:`SafeModeStatus.EXPIRED` transition is named, not piggybacked on
        ``mark_deleted`` with a reason). The system-wide sweep
        (:class:`~backend.workflow.application.safe_mode_expiry.SafeModeExpirySweepRunner`)
        calls this method per row so individual transitions stay observable.

        Returns ``False`` if not found / not in ``PENDING`` or ``EXTENDED`` —
        the edge is enforced, so an already-settled item (approved/denied/
        delivered/archived/deleted/expired) cannot regress to ``EXPIRED``.
        ``decided_at`` is stamped here (the founder didn't decide, but the
        system did — the row LEFT the active queue at this instant).
        """
        row = await self._repo.get(item_id)
        if row is None or row.workspace_id != workspace_id:
            return False
        if row.status not in (SafeModeStatus.PENDING, SafeModeStatus.EXTENDED):
            return False
        row.status = SafeModeStatus.EXPIRED
        row.decided_at = datetime.now(tz=UTC)
        await self._session.flush()
        return True

    async def list_due_expired(self, *, now: datetime | None = None) -> list[SafeModeQueueItemRow]:
        """Every PENDING / EXTENDED row past ``expires_at`` across ALL workspaces.

        System-wide read (no workspace filter) — D3a / M1 plug-in for the
        :class:`backend.workflow.application.safe_mode_expiry.SafeModeExpirySweepRunner`,
        which transitions each returned row to ``EXPIRED`` via
        :meth:`mark_expired` and emits ONE audit-outbox row for the batch (the
        glass-box provenance — ``trigger=schedule``, ``source=system.safe_mode_expiry``).
        Per-workspace callers should keep using :meth:`expire` (single-statement
        update, no audit emission)."""
        return await self._repo.list_due_expired(now=now)

    async def _transition(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
        from_status: SafeModeStatus,
        to_status: SafeModeStatus,
        stamp_decided: bool = True,
        actor_id: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> bool:
        row = await self._repo.get(item_id)
        if row is None or row.workspace_id != workspace_id:
            return False
        if row.status != from_status:
            return False
        row.status = to_status
        # ``decided_at`` marks WHEN the founder settled the item (approve/deny);
        # the post-decision lifecycle transitions (delivered/deleted) preserve
        # that original timestamp rather than overwrite it.
        if stamp_decided:
            row.decided_at = datetime.now(tz=UTC)
            row.decided_by = actor_id
        if reason is not None:
            row.deny_reason = reason
        await self._session.flush()
        return True


__all__ = [
    "EXTENSION_TTL_DAYS",
    "INITIAL_TTL_DAYS",
    "MAX_EXTENSIONS",
    "SafeModeQueue",
]
