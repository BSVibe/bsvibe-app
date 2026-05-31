"""Audit events emitted from the chat completions path.

Per Workflow §6: every gateway dispatch lands an audit event in the
in-transaction outbox; the relay worker drains it asynchronously.
"""

from __future__ import annotations

from typing import ClassVar

from backend.extensions.implementations.audit.events import AuditEventBase


class GatewayCompletionDispatched(AuditEventBase):
    """One chat completion was dispatched through the gateway."""

    DEFAULT_EVENT_TYPE: ClassVar[str] = "gateway.completion.dispatched"


class GatewayCompletionFailed(AuditEventBase):
    """A chat completion attempt failed (dispatch / budget / account)."""

    DEFAULT_EVENT_TYPE: ClassVar[str] = "gateway.completion.failed"


__all__ = ["GatewayCompletionDispatched", "GatewayCompletionFailed"]
