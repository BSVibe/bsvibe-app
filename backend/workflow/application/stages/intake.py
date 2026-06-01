"""Receive stage (B10b) — the gap between intake and Frame (Workflow §0/§1).

The spec's :term:`Receive` stage sits *between* a durable :class:`TriggerEvent`
and the orchestrator's Frame. Its job is small and well-defined:

1. **Resolve the binding** — for connector-inbound triggers, look the
   ``(connector_account_id, resource_id)`` pair up in the B10a
   ``resource_bindings`` table (per-Product × Connector 3-knob row). A miss
   is NOT an error — the trigger falls through to today's behavior (Request
   minted with ``product_id`` unset).
2. **Apply the binding's filter** — ``trigger.filters`` is a simple dict of
   key→value equality clauses for v1. Every key in ``filters`` must equal the
   matching key on the payload for the trigger to pass; any mismatch (or a
   missing key) rejects the trigger as ``filtered_out``.
3. **Populate routing hints** — on a passing match, copy the binding's
   ``product_id`` (and an optional ``selection.artifact_type``) onto the
   Request payload so the downstream Frame stage can use them.

Pass-through cases (no resolution, no filtering) — ``direct`` / ``schedule``
/ ``decision_resolution`` triggers and any trigger lacking the
``connector_account_id`` + ``resource_id`` payload keys.

The module is intentionally pure — no I/O outside the supplied
:class:`AsyncSession`. The :class:`backend.workflow.infrastructure.workers.intake_worker.IntakeWorker`
is the one place that calls :func:`receive`; tests can call it directly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.infrastructure.repositories import (
    SqlAlchemyResourceBindingRepository,
)
from backend.identity.workspaces_db import ResourceBindingRow
from backend.workflow.infrastructure.intake.db import TriggerEventRow, TriggerKind

logger = structlog.get_logger(__name__)


# Payload key the Receive stage writes to mark a TriggerEvent as filter-rejected
# (so the operator can see the trigger landed but was intentionally NOT turned
# into a Request). The value is an honest record dict — see :func:`receive`.
RECEIVE_FILTERED_KEY: str = "_received_filtered"

# Payload keys parsers/dispatch are expected to populate on a connector-inbound
# trigger for the Receive lookup. The keys are namespaced with a leading
# underscore to signal "system metadata, not user payload" (the connector body
# itself lives alongside, e.g. ``action`` / ``github_event`` / ``repo``).
_PAYLOAD_KEY_ACCOUNT_ID: str = "connector_account_id"
_PAYLOAD_KEY_RESOURCE_ID: str = "resource_id"


@dataclass(slots=True)
class ReceiveOutcome:
    """Result of one Receive evaluation.

    ``filtered_out=True`` → caller MUST NOT create a Request for this trigger.
    ``filtered_out=False`` → caller mints the Request using ``request_payload``
    (the original trigger payload, plus any routing hints Receive resolved
    from the binding).
    """

    filtered_out: bool
    request_payload: dict[str, Any] = field(default_factory=dict)
    product_id: uuid.UUID | None = None
    suggested_artifact_type: str | None = None
    binding_id: uuid.UUID | None = None
    reason: str | None = None


def _coerce_account_id(raw: Any) -> uuid.UUID | None:
    """Parse the payload's ``connector_account_id`` into a ``UUID``.

    Returns ``None`` for any non-UUID value (a malformed payload should not
    crash Receive — the trigger just falls through to pass-through).
    """
    if isinstance(raw, uuid.UUID):
        return raw
    if isinstance(raw, str):
        try:
            return uuid.UUID(raw)
        except ValueError:
            return None
    return None


def _filters_match(filters: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Simple AND of key→value equality clauses (Workflow §3 v1 semantics).

    Every key in ``filters`` must be present on ``payload`` with an equal
    value. An empty ``filters`` dict trivially matches (no constraint).
    """
    for key, expected in filters.items():
        if payload.get(key) != expected:
            return False
    return True


def filtered_out_record(*, filters: dict[str, Any], reason: str) -> dict[str, Any]:
    """Build the audit record stamped onto a filter-rejected TriggerEvent row.

    Used by :class:`backend.workflow.infrastructure.workers.intake_worker.IntakeWorker` to mark a
    trigger as "received but rejected by the binding filter" — the operator
    sees the trigger landed, no Request was minted, and *why*.
    """
    return {
        "reason": reason,
        "filters": dict(filters),
        "filtered_at": datetime.now(tz=UTC).isoformat(),
    }


async def receive(session: AsyncSession, trigger: TriggerEventRow) -> ReceiveOutcome:
    """Run the Receive stage for one :class:`TriggerEventRow`.

    See module docstring for the full contract. Never raises on a malformed
    payload — degraded inputs degrade to pass-through (today's behavior).
    """
    payload: dict[str, Any] = dict(trigger.payload or {})

    # Pass-through for non-webhook trigger kinds — direct / schedule /
    # decision_resolution don't have connector bindings.
    # L-P1: forward the trigger's own product_id onto the outcome so the
    # request mint propagates it (the founder-direct path already records
    # product_id on the trigger via DirectTrigger; the webhook path
    # populates it below from the resource binding when a binding matches).
    if trigger.trigger_kind != TriggerKind.WEBHOOK:
        return ReceiveOutcome(
            filtered_out=False,
            request_payload=payload,
            product_id=trigger.product_id,
        )

    account_id = _coerce_account_id(payload.get(_PAYLOAD_KEY_ACCOUNT_ID))
    resource_id_raw = payload.get(_PAYLOAD_KEY_RESOURCE_ID)
    resource_id = resource_id_raw if isinstance(resource_id_raw, str) else None

    # Pass-through for webhook triggers that don't carry the routing keys —
    # e.g. an inbound parser that hasn't been retrofitted yet. L-P1: forward
    # the trigger's own product_id so it isn't dropped on the floor.
    if account_id is None or resource_id is None:
        return ReceiveOutcome(
            filtered_out=False,
            request_payload=payload,
            product_id=trigger.product_id,
        )

    repo = SqlAlchemyResourceBindingRepository(session)
    binding: ResourceBindingRow | None = await repo.find_binding(
        connector_account_id=account_id, resource_id=resource_id
    )

    # No binding for this (account, resource) pair → pass-through. L-P1:
    # carry the trigger's product_id forward when set (a webhook can land
    # without a binding match yet still know its target product if the
    # producer recorded it; honor that intent rather than minting NULL).
    if binding is None:
        logger.info(
            "receive_no_binding",
            workspace_id=str(trigger.workspace_id),
            connector_account_id=str(account_id),
            resource_id=resource_id,
        )
        return ReceiveOutcome(
            filtered_out=False,
            request_payload=payload,
            product_id=trigger.product_id,
        )

    # Apply the binding's filter (simple key-equality AND, Workflow §3 v1).
    trig_cfg = dict(binding.trigger or {})
    filters = dict(trig_cfg.get("filters") or {})
    if filters and not _filters_match(filters, payload):
        logger.info(
            "receive_filtered_out",
            workspace_id=str(trigger.workspace_id),
            connector_account_id=str(account_id),
            resource_id=resource_id,
            binding_id=str(binding.id),
            filters=filters,
        )
        return ReceiveOutcome(
            filtered_out=True,
            reason="filter_rejected",
            binding_id=binding.id,
            request_payload=payload,
        )

    # PASS — populate routing hints from the binding's selection scope.
    selection = dict(binding.selection or {})
    artifact_type_raw = selection.get("artifact_type")
    suggested_artifact_type = artifact_type_raw if isinstance(artifact_type_raw, str) else None

    enriched: dict[str, Any] = dict(payload)
    enriched["product_id"] = str(binding.product_id)
    enriched["binding_id"] = str(binding.id)
    enriched["selection"] = selection
    if suggested_artifact_type is not None:
        enriched["suggested_artifact_type"] = suggested_artifact_type

    logger.info(
        "receive_pass",
        workspace_id=str(trigger.workspace_id),
        connector_account_id=str(account_id),
        resource_id=resource_id,
        binding_id=str(binding.id),
        product_id=str(binding.product_id),
        suggested_artifact_type=suggested_artifact_type,
    )
    return ReceiveOutcome(
        filtered_out=False,
        request_payload=enriched,
        product_id=binding.product_id,
        suggested_artifact_type=suggested_artifact_type,
        binding_id=binding.id,
    )


__all__ = [
    "RECEIVE_FILTERED_KEY",
    "ReceiveOutcome",
    "filtered_out_record",
    "receive",
]
