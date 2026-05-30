"""SafeModeCompensationHook — fire compensation on settle-without-delivery.

D3b (this PR). Workflow §10.5 + §3.1. When a Safe Mode queue row reaches a
terminal state WITHOUT being delivered out (the founder denied the queued
delivery, or the retention window expired before the founder decided), the
underlying Deliverable may still be in a state that warrants compensation —
a newer supersede candidate may exist, a verification regression may have
flipped the run, or the deliverable is direct-output and needs notification.

D3 (PR #215) wired :class:`~backend.delivery.compensation.CompensationHandler`
into ONLY the ``/deliverables/{id}/retract`` API path (and even there via the
plugin ``@p.compensate`` handlers — a different layer). D3a (PR #222) shipped
the expiry sweep + per-batch ``safe_mode.expired`` audit hook but explicitly
left auto-compensation wiring to D3b. This module IS that wiring.

**Direct in-process call, not subscriber-based.** D3a's docstring anticipated
"D3b just adds a subscriber" to the audit hook. After investigating the
audit substrate, the subscriber path is NOT cheap today: the only outbox
consumer is :class:`~backend.workers.relay_worker.RelayWorker`, which drains
to an EXTERNAL sink (HTTP/gRPC relay) — there is no in-process event-bus
subscriber framework that fans audit rows out to local handlers. Inventing
that fan-out is a separate lift. Calling :class:`CompensationHandler` directly
at the deny site and at the sweep loop end keeps D3b a one-PR change and
preserves the property the prompt cares about: compensation fires exactly
once per settle-without-delivery transition, in the same request/tick as the
transition itself.

**Per-item granularity.** The sweep emits ONE audit row per BATCH (D3a, by
design — "a thousand rows expiring in one tick is one operational event").
Compensation, however, is per-DELIVERABLE — the supersede/revert/notify
decision depends on the deliverable's own state. So the per-item fan-out
happens HERE, on the sweep's expired-ids list, not at the audit subscriber.

**Soft-fail.** The hook swallows + logs any exception from
:meth:`CompensationHandler.evaluate`. The lifecycle flip has already happened
(the row is denied/expired in the DB); a flaky compensation evaluator must
not silently revert the founder's decision. The hook is best-effort; a
follow-up sweep / a retract-path retry remains available.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.delivery.compensation import CompensationHandler

logger = structlog.get_logger(__name__)


async def fire_compensation_for_item(
    session: AsyncSession,
    *,
    deliverable_id: uuid.UUID,
    trigger: str,
) -> None:
    """Evaluate compensation for one Deliverable settled-without-delivery.

    ``trigger`` is a glass-box tag for the log line — ``deny`` or ``expire``
    so the operator can tell why the hook fired. The result of
    :meth:`CompensationHandler.evaluate` is logged but not raised; the caller
    (the lifecycle method or the sweep loop) cannot meaningfully recover
    from a downstream blip mid-transition.
    """
    try:
        handler = CompensationHandler(session)
        result = await handler.evaluate(deliverable_id=deliverable_id)
    except Exception as exc:  # noqa: BLE001 — best-effort hook; never break the transition
        logger.warning(
            "safe_mode_compensation_hook_failed",
            deliverable_id=str(deliverable_id),
            trigger=trigger,
            error=str(exc),
            exc_info=True,
        )
        return

    logger.info(
        "safe_mode_compensation_hook_fired",
        deliverable_id=str(deliverable_id),
        trigger=trigger,
        action=result.action if result is not None else None,
        reason=result.reason if result is not None else None,
    )


__all__ = ["fire_compensation_for_item"]
