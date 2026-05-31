"""Audit events emitted by :class:`PresetService`."""

from __future__ import annotations

from typing import ClassVar

from plugin.audit.events import AuditEventBase


class PresetAppliedEvent(AuditEventBase):
    """Emitted in the same transaction that materializes the preset."""

    DEFAULT_EVENT_TYPE: ClassVar[str | None] = "gateway.preset.applied"
