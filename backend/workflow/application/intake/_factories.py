"""Internal factories for intake aggregates.

Lift I-Repo-Workflow-3. The intake receivers (:class:`WebhookReceiver`,
:class:`DirectTrigger`) used to instantiate :class:`TriggerEventRow`
inline. Centralising the construction behind a tiny factory function
removes the last direct ORM reference from those callers — they now go
through this module + the :class:`IdempotencyRepository` Protocol — and
keeps the field-set / default arguments in one place.

This is a deliberately thin seam: the ORM type stays canonical (no
domain-entity split yet, matching the Workflow-context pragmatism in
:mod:`backend.workflow.domain.repositories`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from backend.workflow.infrastructure.intake.db import TriggerEventRow, TriggerKind


def _new_trigger_row(
    *,
    workspace_id: uuid.UUID,
    source: str,
    kind: TriggerKind,
    idem: str,
    payload: dict[str, Any],
    received_at: datetime,
    product_id: uuid.UUID | None = None,
    trace_id: str | None = None,
) -> TriggerEventRow:
    """Build a TriggerEventRow with consistent field ordering across receivers."""
    return TriggerEventRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=product_id,
        source=source,
        trigger_kind=kind,
        idempotency_key=idem,
        payload=payload,
        trace_id=trace_id,
        received_at=received_at,
    )


__all__ = ["_new_trigger_row"]
