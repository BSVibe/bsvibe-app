"""Wire-shape pydantic models for audit events.

Lifted from ``bsvibe_audit.events.base`` with the import path rewritten.
Producer code stays type-safe — ``extra='forbid'`` makes typos fail at
emit time, not silently disappear into a JSON blob.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

ActorType = Literal["user", "service", "system"]


class AuditActor(BaseModel):
    """Who performed the action recorded by the event."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    type: ActorType
    id: str
    email: str | None = None
    label: str | None = None


class AuditResource(BaseModel):
    """Reference to the resource the event is about (optional)."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    type: str
    id: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AuditEventBase(BaseModel):
    """Wire shape every audit event extends."""

    model_config = ConfigDict(extra="forbid")

    DEFAULT_EVENT_TYPE: ClassVar[str | None] = None

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = ""
    occurred_at: datetime = Field(default_factory=_utcnow)
    actor: AuditActor
    tenant_id: str | None = None
    workspace_id: str | None = None
    trace_id: str | None = None
    resource: AuditResource | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    def __init__(self, **values: Any) -> None:
        if "event_type" not in values:
            cls_default = type(self).DEFAULT_EVENT_TYPE
            if cls_default is not None:
                values["event_type"] = cls_default
        super().__init__(**values)
