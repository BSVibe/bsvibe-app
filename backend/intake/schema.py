"""Intake schemas — Workflow §3.1 ``TriggerEvent`` and friends.

Per Workflow §12.5 #8 (Bundle G — Intake / Triggers), this module defines
the inbound trigger envelope every intake surface (webhook / schedule /
direct / decision-resolution) produces. Downstream the orchestrator
(Bundle G — Orchestrator) consumes :class:`TriggerEvent` rows and turns
them into ``Request`` rows for the workflow state machine.

Field shapes are anchored in Workflow §3 (TriggerEvent / DeliveryResult /
ActionResult) so that producer/consumer drift is impossible without an
explicit schema bump.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

logger = structlog.get_logger(__name__)


TriggerKindLiteral = Literal["webhook", "schedule", "direct", "decision_resolution"]


class TriggerEvent(BaseModel):
    """Inbound trigger envelope — Workflow §3.1.

    A trigger represents the *outside* of the system asking us to do
    something. It is the only legal way to enter the workflow state
    machine (Workflow §1, 3+ε stages).

    Idempotency is enforced at intake time via the composite
    ``(workspace_id, source, idempotency_key)`` key — see
    :mod:`backend.intake.idempotency`.
    """

    workspace_id: uuid.UUID
    source: str  # plugin name OR "schedule" OR "direct" OR "decision_resolution"
    trigger_kind: TriggerKindLiteral
    idempotency_key: str
    payload: dict[str, Any]

    product_id: uuid.UUID | None = None
    trace_id: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now())

    model_config = ConfigDict(extra="forbid")


__all__ = ["TriggerEvent", "TriggerKindLiteral"]
