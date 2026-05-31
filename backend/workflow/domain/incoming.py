"""Intake schemas — Workflow §3.1 ``TriggerEvent`` and friends.

Per Workflow §12.5 #8 (Bundle G — Intake / Triggers), this module defines
the inbound trigger envelope every intake surface (webhook / schedule /
direct / decision-resolution) produces. Downstream the orchestrator
(Bundle G — Orchestrator) consumes :class:`TriggerEvent` rows and turns
them into ``Request`` rows for the workflow state machine.

Field shapes are anchored in Workflow §3 (TriggerEvent / DeliveryResult /
ActionResult) so that producer/consumer drift is impossible without an
explicit schema bump. The B10b cohort completes the spec field set:
``connector`` / ``connector_account_id`` / ``resource_id`` /
``suggested_artifact_type`` / ``suggested_skill`` / ``intent_text`` /
``actor`` / ``correlation_id``. They are all optional so legacy producers
that only emit the original Bundle G fields continue to validate.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

logger = structlog.get_logger(__name__)


TriggerKindLiteral = Literal["webhook", "schedule", "direct", "decision_resolution"]
ActorLiteral = Literal["founder", "external", "system"]


class TriggerEvent(BaseModel):
    """Inbound trigger envelope — Workflow §3.1.

    A trigger represents the *outside* of the system asking us to do
    something. It is the only legal way to enter the workflow state
    machine (Workflow §1, 3+ε stages).

    Idempotency is enforced at intake time via the composite
    ``(workspace_id, source, idempotency_key)`` key — see
    :mod:`backend.workflow.infrastructure.idempotency`.

    Routing hints (``connector`` / ``connector_account_id`` / ``resource_id``
    / ``suggested_artifact_type`` / ``suggested_skill``) are populated by the
    Receive stage (Workflow §0 / §1, B10b) and refined by Frame; see
    :mod:`backend.workflow.application.stages.intake`.
    """

    workspace_id: uuid.UUID
    source: str  # plugin name OR "schedule" OR "direct" OR "decision_resolution"
    trigger_kind: TriggerKindLiteral
    idempotency_key: str
    payload: dict[str, Any]

    product_id: uuid.UUID | None = None
    trace_id: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now())

    # --- B10b additions: spec §3.1 routing hints ---
    # Connector identity (only meaningful for inbound webhook deliveries).
    connector: str | None = None
    connector_account_id: uuid.UUID | None = None
    resource_id: str | None = None

    # Routing hints — Receive sets these from the binding; Frame refines them.
    suggested_artifact_type: str | None = None
    suggested_skill: str | None = None

    # Content + provenance.
    intent_text: str | None = None
    actor: ActorLiteral = "external"
    correlation_id: uuid.UUID | None = None

    model_config = ConfigDict(extra="forbid")


__all__ = ["ActorLiteral", "TriggerEvent", "TriggerKindLiteral"]
