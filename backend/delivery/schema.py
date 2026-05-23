"""Delivery schemas — Workflow §3.1 ``DeliveryResult`` / ``ActionResult``.

Workflow §12.5 #8 (Bundle G — Delivery). When a deliverable is shipped
the delivery dispatcher fans the artifact out to plugin outbound
adapters; each adapter returns an :class:`ActionResult` and the
aggregate is the :class:`DeliveryResult` we persist + surface.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

logger = structlog.get_logger(__name__)


ArtifactType = Literal[
    "code",
    "pr",
    "page",
    "page_image",
    "direct_output",
    "slack_message",
    "email",
    "telegram_message",
    "discord_message",
    "issue",
    "card",
]
"""Workflow §3.1 — the canonical artifact-type tags downstream
plugins understand.

The first five mirror :class:`~backend.execution.db.DeliverableType` 1:1 (the
*deliverable's own* type). The rest are connector-outbound dispatch tags: a
connector's ``@p.outbound`` declares the artifact_type it accepts (slack →
``slack_message``, email-sender → ``email``, telegram → ``telegram_message``,
discord → ``discord_message``, linear → ``issue``, trello → ``card``), and the
connector event-builders in :mod:`backend.delivery.connector_dispatch` dispatch
the shaped event under that tag so the dispatcher's
``artifact_type in cap.artifact_types`` match selects the right outbound."""


CompensationAction = Literal["revert", "supersede", "notify"]


class ActionResult(BaseModel):
    """Per-action outcome inside one delivery fan-out — Workflow §3.1."""

    action: str
    succeeded: bool
    output: dict[str, Any] | None = None
    error: str | None = None

    model_config = ConfigDict(extra="forbid")


class DeliveryResult(BaseModel):
    """Aggregate result of one dispatched deliverable — Workflow §3.1."""

    workspace_id: uuid.UUID
    deliverable_id: uuid.UUID
    artifact_type: ArtifactType
    actions: list[ActionResult]
    delivered_at: datetime = Field(default_factory=lambda: datetime.now())
    error: str | None = None

    model_config = ConfigDict(extra="forbid")


class CompensationResult(BaseModel):
    """Outcome of a compensation evaluation — Workflow §3.1 / §10.5."""

    deliverable_id: uuid.UUID
    action: CompensationAction
    reason: str

    model_config = ConfigDict(extra="forbid")


__all__ = [
    "ActionResult",
    "ArtifactType",
    "CompensationAction",
    "CompensationResult",
    "DeliveryResult",
]
