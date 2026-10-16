"""Tell the founder when a host worker can no longer sign in to Claude (and when it can again).

Measured 2026-10-15: the worker's Claude OAuth refresh token died (``invalid_grant``) and every
agent turn on it failed in one second with no usage. The keepalive already classified the
failure as definitive and logged it hourly — for ~17 hours, unread. The founder found out only
because a probe run failed.

The worker sends its verdict on every heartbeat as ``X-BSVibe-Worker-Claude-Auth`` (``ok`` /
``failed``); :func:`note_claude_auth` turns the TRANSITION into one ``worker_auth_down``
notification through the ordinary outbox. ``claude_auth_failed_at`` on the worker row is the
memory: a repeat ``failed`` is a no-op, and an ``ok`` announces a recovery only when a failure
was announced. No header (an older worker) or a value this does not know changes nothing — the
heartbeat is the worker's lifeline and must never be refused over this.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.data.rls import workspace_session_scope
from backend.executors.db import WorkerRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.notifications.copy import worker_auth_copy
from backend.notifications.emit import emit_notification

logger = structlog.get_logger(__name__)

#: Must be listed in ``NOTIFICATION_OUTBOX.producers`` (the INV-1 producer guard).
PRODUCER_ID = "api:worker_heartbeat"

#: Where the alert links: Settings → Models lists the founder's workers.
WORKER_AUTH_LINK = "/settings/models"

_STATES = frozenset({"ok", "failed"})


async def note_claude_auth(session: AsyncSession, worker: WorkerRow, state: str | None) -> None:
    """Record ``worker``'s reported Claude sign-in ``state`` and alert on a transition.

    Flushes; the caller (the heartbeat route) commits, so the alert is staged in the same
    transaction as the stamp — both land or neither does.
    """
    if state not in _STATES:
        if state is not None:
            logger.info("worker_claude_auth_header_ignored", worker_id=str(worker.id), value=state)
        return
    failed_at = worker.claude_auth_failed_at
    if state == "failed" and failed_at is None:
        worker.claude_auth_failed_at = datetime.now(UTC)
        await _announce(session, worker, episode=worker.claude_auth_failed_at, recovered=False)
    elif state == "ok" and failed_at is not None:
        worker.claude_auth_failed_at = None
        await _announce(session, worker, episode=failed_at, recovered=True)
    await session.flush()


async def _announce(
    session: AsyncSession, worker: WorkerRow, *, episode: datetime, recovered: bool
) -> None:
    """Stage the alert for one outage ``episode`` (keyed by when it began): its down and its up
    are each sent once, and a later outage is a new episode with its own pair."""
    async with workspace_session_scope(session, worker.workspace_id):
        workspace = await session.get(WorkspaceRow, worker.workspace_id)
        copy = worker_auth_copy(
            workspace.language if workspace else None,
            worker_name=worker.name,
            recovered=recovered,
        )
        await emit_notification(
            session,
            workspace_id=worker.workspace_id,
            event="worker_auth_down",
            dedupe_key=(
                f"worker_auth_down:{worker.id}:{episode.isoformat()}:{'up' if recovered else 'down'}"
            ),
            payload={
                "title": copy.title,
                "body": copy.body,
                "link": WORKER_AUTH_LINK,
                "recovered": recovered,
                "worker_id": str(worker.id),
            },
            producer_id=PRODUCER_ID,
        )
    logger.warning(
        "worker_claude_auth_recovered" if recovered else "worker_claude_auth_down",
        worker_id=str(worker.id),
        worker_name=worker.name,
    )


__all__ = ["PRODUCER_ID", "WORKER_AUTH_LINK", "note_claude_auth"]
