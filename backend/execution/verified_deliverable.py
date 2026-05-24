"""Shared verified-terminal artifact writes — ONE source of truth.

Workflow §1 (verified terminal) / §11.3. When a run reaches ``verified`` the
backend must land a stable, well-known artifact contract regardless of HOW the
work was produced:

* the native agent loop (:class:`~backend.execution.orchestrator.RunOrchestrator`),
* or an external CLI worker (:class:`~backend.executors.orchestrator.ExecutorOrchestrator`,
  Lift 5b of the executor-pool epic).

Both paths call :func:`write_verified_deliverable` so the
``Deliverable`` / ``DeliveryEventRow`` / settle-activity shape has a single
definition — diverging the artifact shape across compute backends would silently
break every downstream consumer (DeliveryWorker, SettleWorker, the PWA Brief).

This helper is the *write* contract only — it does NOT transition the WorkStep /
RunAttempt rows nor emit the Redis wake-up notifications (those stay
caller-owned, since each compute path owns its own attempt bookkeeping and the
native loop's stream emission is gated on its own redis client). It ``add``s +
``flush``es but never ``commit``s (the caller owns the transaction boundary).
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    ExecutionRunActivity,
)

logger = structlog.get_logger(__name__)

_SETTLE_SUMMARY_CAP = 500


async def settle_run_context(session: AsyncSession, run: ExecutionRun) -> dict[str, Any]:
    """Resolve the run's stable settle-clustering context.

    ``intent_text`` is the founder's own Direction (set by intake); the product
    slug/name is the run's product binding (resolved from ``run.product_id``).
    Both are stable inputs the SettleWorker uses as canonicalization cluster
    keys — never the work LLM's / worker's free output. Only present keys are
    returned (a connector-inbound run with no product/intent yields ``{}``).
    Resolution is best-effort — a missing/deleted product row is omitted, never
    an exception that could break the verified terminal.
    """
    context: dict[str, Any] = {}
    intent_text = (run.payload or {}).get("intent_text")
    if isinstance(intent_text, str) and intent_text.strip():
        context["intent_text"] = intent_text
    if run.product_id is not None:
        from backend.workspaces.db import ProductRow  # noqa: PLC0415 — cross-domain, local

        product = await session.get(ProductRow, run.product_id)
        if product is not None:
            context["product_slug"] = product.slug
            context["product_name"] = product.name
    return context


async def write_verified_deliverable(
    session: AsyncSession,
    run: ExecutionRun,
    *,
    attempt_id: uuid.UUID,
    artifact_refs: list[str],
    summary: str,
) -> Deliverable:
    """Write the verified-terminal artifacts for ``run`` and return the Deliverable.

    Emits exactly what the native loop's ``_finish_verified`` has always written:

    1. a CODE :class:`Deliverable` (``payload={"artifact_refs", "summary"}``),
    2. a :class:`DeliveryEventRow` (drained by the DeliveryWorker), and
    3. a ``settle`` :class:`ExecutionRunActivity` carrying the run's stable
       clustering context (intent/product) + ``verified: True``.

    The summary is truncated to :data:`_SETTLE_SUMMARY_CAP` chars in the deliver
    event + settle payloads (matching the native path); the Deliverable itself
    keeps the full summary.
    """
    deliverable = Deliverable(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=run.workspace_id,
        deliverable_type=DeliverableType.CODE,
        artifact_uri=None,
        diff_url=None,
        payload={"artifact_refs": artifact_refs, "summary": summary},
    )
    session.add(deliverable)
    await session.flush()

    # Deliver event — drained by the DeliveryWorker (delivery_events table).
    from backend.delivery.db import DeliveryEventRow  # noqa: PLC0415 — cross-domain, local

    session.add(
        DeliveryEventRow(
            id=uuid.uuid4(),
            workspace_id=run.workspace_id,
            deliverable_id=deliverable.id,
            artifact_type=DeliverableType.CODE.value,
            payload={"artifact_refs": artifact_refs, "summary": summary[:_SETTLE_SUMMARY_CAP]},
        )
    )

    # Settle observation — the run-trace/observation side channel (§1).
    settle_payload: dict[str, Any] = {
        "attempt_id": str(attempt_id),
        "verified": True,
        "artifact_refs": artifact_refs,
        "summary": summary[:_SETTLE_SUMMARY_CAP],
        **await settle_run_context(session, run),
    }
    session.add(
        ExecutionRunActivity(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            activity_type="settle",
            payload=settle_payload,
        )
    )
    await session.flush()
    logger.info(
        "verified_deliverable_written",
        run_id=str(run.id),
        deliverable_id=str(deliverable.id),
        artifact_refs=artifact_refs,
    )
    return deliverable


__all__ = ["settle_run_context", "write_verified_deliverable"]
