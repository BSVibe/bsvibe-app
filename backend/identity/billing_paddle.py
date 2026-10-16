"""Paddle subscription webhooks → workspace plans (#928 step 2).

Capacity subscription (founder decision 2026-10-16): Paddle is the Merchant of Record, and a
subscription's status decides which plan its workspace is on. This module is the whole
translation; :func:`backend.identity.plans.apply_plan` is the only thing it writes plans with.

Signature — Paddle Billing signs each delivery with the notification destination's secret:
``Paddle-Signature: ts=<unix>;h1=<hex HMAC-SHA256(secret, "<ts>:<raw body>")>``. Several ``h1``
values may be present while a secret rotates; any match passes. The timestamp bounds replay of
a captured delivery.

Status → plan:

* ``active`` / ``trialing`` on a configured Pro price → Pro
* ``canceled`` / ``paused`` → Free
* ``past_due`` → no change (Paddle is still collecting; it sends ``canceled`` if it gives up)

Which workspace: the FIRST event names it through ``custom_data.workspace_id`` (set by our
checkout) and binds the subscription id to it; every later event is resolved by the stored
subscription id, so client-set ``custom_data`` cannot re-point a paid subscription.

Paddle retries and does not order deliveries: an event id already recorded is acknowledged
and skipped, and an event older than the last one applied to its workspace is ignored. The
operator's workspace is never moved by a payment.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.data.rls import cross_tenant_session_read, workspace_session_scope
from backend.identity.plans import Plan, apply_plan
from backend.identity.workspaces_db import BillingWebhookEventRow, WorkspaceRow

logger = structlog.get_logger(__name__)

_TO_PRO = frozenset({"active", "trialing"})
_TO_FREE = frozenset({"canceled", "paused"})


def verify_signature(
    header: str | None, body: bytes, *, secret: str, now: float, tolerance_s: float
) -> bool:
    """True iff ``header`` is a fresh Paddle signature of ``body`` under ``secret``."""
    if not header or not secret:
        return False
    parts: dict[str, list[str]] = {}
    for piece in header.split(";"):
        key, _, value = piece.strip().partition("=")
        parts.setdefault(key, []).append(value)
    stamps = parts.get("ts") or []
    if len(stamps) != 1 or not stamps[0].isdigit():
        return False
    if abs(now - int(stamps[0])) > tolerance_s:
        return False
    expected = hmac.new(
        secret.encode(), f"{stamps[0]}:".encode() + body, hashlib.sha256
    ).hexdigest()
    return any(hmac.compare_digest(expected, given) for given in parts.get("h1") or [])


@dataclass(frozen=True)
class SubscriptionEvent:
    event_id: str
    event_type: str
    occurred_at: datetime
    subscription_id: str
    customer_id: str | None
    status: str
    price_ids: frozenset[str]
    workspace_id: uuid.UUID | None


def parse_subscription_event(payload: dict[str, Any]) -> SubscriptionEvent | None:
    """The subscription event in ``payload``, or ``None`` for any other event type."""
    event_type = str(payload.get("event_type") or "")
    if not event_type.startswith("subscription."):
        return None
    data = payload.get("data") or {}
    custom = data.get("custom_data") or {}
    try:
        workspace_id = (
            uuid.UUID(str(custom["workspace_id"])) if custom.get("workspace_id") else None
        )
    except ValueError:
        workspace_id = None
    occurred = datetime.fromisoformat(str(payload["occurred_at"]).replace("Z", "+00:00"))
    return SubscriptionEvent(
        event_id=str(payload["event_id"]),
        event_type=event_type,
        occurred_at=occurred if occurred.tzinfo else occurred.replace(tzinfo=UTC),
        subscription_id=str(data["id"]),
        customer_id=data.get("customer_id"),
        status=str(data.get("status") or ""),
        price_ids=frozenset(
            str((item.get("price") or {}).get("id")) for item in data.get("items") or []
        ),
        workspace_id=workspace_id,
    )


def target_plan(event: SubscriptionEvent, *, pro_price_ids: frozenset[str]) -> Plan | None:
    """The plan ``event`` puts its workspace on, or ``None`` for "leave it as it is"."""
    if event.status in _TO_PRO:
        return Plan.PRO if event.price_ids & pro_price_ids else None
    if event.status in _TO_FREE:
        return Plan.FREE
    return None


async def _already_seen(session: AsyncSession, event_id: str) -> bool:
    if await session.get(BillingWebhookEventRow, event_id) is not None:
        return True
    session.add(BillingWebhookEventRow(event_id=event_id, received_at=datetime.now(UTC)))
    await session.flush()
    return False


async def _resolve_workspace(
    session: AsyncSession, event: SubscriptionEvent
) -> tuple[uuid.UUID | None, bool]:
    """``(workspace id, already bound)`` — the stored subscription wins over ``custom_data``."""
    async with cross_tenant_session_read(session):
        bound = await session.scalar(
            select(WorkspaceRow.id).where(
                WorkspaceRow.billing_subscription_id == event.subscription_id
            )
        )
        if bound is not None:
            return bound, True
        if event.workspace_id is None:
            return None, False
        exists = await session.scalar(
            select(WorkspaceRow.id).where(WorkspaceRow.id == event.workspace_id)
        )
    return exists, False


async def handle_subscription_event(
    session: AsyncSession, event: SubscriptionEvent, *, pro_price_ids: frozenset[str]
) -> str:
    """Apply ``event``; returns the outcome as a log word. Flushes; the caller commits."""
    if await _already_seen(session, event.event_id):
        return "duplicate"
    workspace_id, bound = await _resolve_workspace(session, event)
    if workspace_id is None:
        logger.error(
            "paddle_webhook_workspace_unknown",
            event_id=event.event_id,
            subscription_id=event.subscription_id,
        )
        return "unknown_workspace"
    async with workspace_session_scope(session, workspace_id):
        row = await session.get(WorkspaceRow, workspace_id)
        assert row is not None  # resolved above, same transaction
        last = row.billing_event_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)  # SQLite hands timestamptz back naive
        if last is not None and event.occurred_at <= last:
            return "stale"
        if row.plan == Plan.OPERATOR.value:
            logger.warning("paddle_webhook_operator_untouched", workspace_id=str(workspace_id))
            return "operator"
        values: dict[str, Any] = {
            "billing_status": event.status,
            "billing_event_at": event.occurred_at,
        }
        if not bound:
            values["billing_subscription_id"] = event.subscription_id
        if event.customer_id:
            values["billing_customer_id"] = event.customer_id
        await session.execute(
            update(WorkspaceRow).where(WorkspaceRow.id == workspace_id).values(**values)
        )
        plan = target_plan(event, pro_price_ids=pro_price_ids)
        if plan is not None:
            await apply_plan(session, workspace_id=workspace_id, plan=plan)
    logger.info(
        "paddle_webhook_applied",
        event_type=event.event_type,
        workspace_id=str(workspace_id),
        status=event.status,
        plan=plan.value if plan else None,
    )
    return "applied"


__all__ = [
    "SubscriptionEvent",
    "handle_subscription_event",
    "parse_subscription_event",
    "target_plan",
    "verify_signature",
]
