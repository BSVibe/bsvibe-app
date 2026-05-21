"""SafeModeQueue — founder approval gate for outbound deliveries.

Workflow §12.5 #8 (Bundle G — Delivery) and Workflow §10.5 (Safe
Mode). When the workspace is in Safe Mode, every deliverable goes
into this queue instead of being auto-dispatched; the founder approves
or denies from the queue UI, and the dispatcher only runs on approval.

Retention window (Workflow §10.5):

* Initial active window: **90 days** from enqueue
* Per-item extension: **+30 days**, max **2** extensions
* After expiry: item flips to ``expired`` status (no further action)
"""

from __future__ import annotations

import uuid

import structlog

logger = structlog.get_logger(__name__)


class SafeModeQueue:
    """Pull-based approval queue for outbound deliveries.

    All methods are stubs — see
    ``backend.delivery.db.SafeModeQueueItemRow`` for the persistence
    contract and the 90d + 2×30d retention envelope per Workflow §10.5.
    """

    async def enqueue(
        self,
        *,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
    ) -> uuid.UUID:
        """Enqueue a pending delivery; returns the queue item id."""
        # TODO(bundle-g-integration): INSERT INTO safe_mode_queue_items
        # with status=pending and expires_at = now() + 90d.
        logger.debug(
            "safe_mode_enqueue_stub",
            workspace_id=str(workspace_id),
            deliverable_id=str(deliverable_id),
        )
        raise NotImplementedError("SafeModeQueue.enqueue pending Bundle G integration")

    async def list_pending(self, *, workspace_id: uuid.UUID) -> list[dict]:
        """Founder-facing list of items awaiting approval."""
        # TODO(bundle-g-integration): SELECT ... WHERE status='pending'
        # ORDER BY created_at DESC.
        logger.debug(
            "safe_mode_list_pending_stub",
            workspace_id=str(workspace_id),
        )
        raise NotImplementedError("SafeModeQueue.list_pending pending Bundle G integration")

    async def approve(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> None:
        """Founder approve — status→approved, hand off to dispatcher."""
        # TODO(bundle-g-integration): transactional flip + DeliveryDispatcher.dispatch.
        logger.debug(
            "safe_mode_approve_stub",
            workspace_id=str(workspace_id),
            item_id=str(item_id),
            actor_id=str(actor_id),
        )
        raise NotImplementedError("SafeModeQueue.approve pending Bundle G integration")

    async def deny(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
        actor_id: uuid.UUID,
        reason: str,
    ) -> None:
        """Founder deny — status→denied, record rationale."""
        # TODO(bundle-g-integration): update + audit_relay.emit.
        logger.debug(
            "safe_mode_deny_stub",
            workspace_id=str(workspace_id),
            item_id=str(item_id),
            actor_id=str(actor_id),
            reason_chars=len(reason),
        )
        raise NotImplementedError("SafeModeQueue.deny pending Bundle G integration")

    async def expire(self, *, workspace_id: uuid.UUID) -> int:
        """Sweep pending items past their ``expires_at``; returns count."""
        # TODO(bundle-g-integration): cron-driven sweeper. Items past
        # the 90d active window without any of 2×30d extensions flip
        # to ``expired``. Returns the number of items flipped.
        logger.debug(
            "safe_mode_expire_stub",
            workspace_id=str(workspace_id),
        )
        raise NotImplementedError("SafeModeQueue.expire pending Bundle G integration")


__all__ = ["SafeModeQueue"]
