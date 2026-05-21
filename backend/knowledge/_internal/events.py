"""EventBus — unified event system for real-time streaming."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)


class EventType(Enum):
    """All event types that can be emitted through the EventBus."""

    # Plugin lifecycle
    PLUGIN_RUN_START = "plugin_run_start"
    PLUGIN_RUN_COMPLETE = "plugin_run_complete"
    PLUGIN_RUN_ERROR = "plugin_run_error"

    # Skill lifecycle
    SKILL_RUN_START = "skill_run_start"
    SKILL_GATHER_COMPLETE = "skill_gather_complete"
    SKILL_LLM_RESPONSE = "skill_llm_response"
    SKILL_APPLY_COMPLETE = "skill_apply_complete"
    SKILL_RUN_COMPLETE = "skill_run_complete"
    SKILL_RUN_ERROR = "skill_run_error"

    # Vault writes
    SEED_WRITTEN = "seed_written"
    GARDEN_WRITTEN = "garden_written"
    ACTION_LOGGED = "action_logged"

    # Vault mutations
    NOTE_UPDATED = "note_updated"
    NOTE_DELETED = "note_deleted"

    # Scheduler
    TRIGGER_FIRED = "trigger_fired"

    # Tool calls (from AgentLoop)
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_COMPLETE = "tool_call_complete"

    # Agent loop
    INPUT_RECEIVED = "input_received"
    INPUT_COMPLETE = "input_complete"

    # Ingest compiler
    INGEST_COMPILE_START = "ingest_compile_start"
    INGEST_COMPILE_COMPLETE = "ingest_compile_complete"
    INGEST_COMPILE_BATCH_START = "ingest_compile_batch_start"
    INGEST_COMPILE_BATCH_CHUNK_START = "ingest_compile_batch_chunk_start"
    INGEST_COMPILE_BATCH_CHUNK_DONE = "ingest_compile_batch_chunk_done"
    INGEST_COMPILE_BATCH_CHUNK_FAILED = "ingest_compile_batch_chunk_failed"
    INGEST_COMPILE_BATCH_COMPLETE = "ingest_compile_batch_complete"

    # Credentials
    CREDENTIAL_SETUP_REQUIRED = "credential_setup_required"

    # Canonicalization (Handoff §14)
    CANONICALIZATION_PROPOSAL_CREATED = "canonicalization_proposal_created"
    CANONICALIZATION_PROPOSAL_STATUS_CHANGED = "canonicalization_proposal_status_changed"
    CANONICALIZATION_ACTION_DRAFTED = "canonicalization_action_drafted"
    CANONICALIZATION_ACTION_STATUS_CHANGED = "canonicalization_action_status_changed"
    CANONICALIZATION_ACTION_APPLIED = "canonicalization_action_applied"
    CANONICALIZATION_DECISION_CREATED = "canonicalization_decision_created"
    CANONICALIZATION_DECISION_SUPERSEDED = "canonicalization_decision_superseded"
    CANONICALIZATION_POLICY_UPDATED = "canonicalization_policy_updated"
    CANONICALIZATION_POLICY_CONFLICT = "canonicalization_policy_conflict"


@dataclass
class Event:
    """Structured event emitted by BSage components.

    Attributes:
        event_type: Classification of the event.
        payload: Event-specific data.
        correlation_id: Groups related events (e.g., all events from one plugin run).
        timestamp: ISO-format UTC timestamp when the event occurred.
    """

    event_type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict for WebSocket broadcast."""
        return {
            "type": "event",
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
        }


@runtime_checkable
class EventSubscriber(Protocol):
    """Protocol for objects that receive events from the EventBus."""

    async def on_event(self, event: Event) -> None: ...


async def emit_event(
    event_bus: EventBus | None,
    event_name: str,
    payload: dict[str, Any],
    *,
    correlation_id: str = "",
) -> None:
    """Emit an event to the EventBus, if configured.

    Shared helper that replaces per-component ``_emit`` methods.

    Args:
        event_bus: The EventBus instance, or None to no-op.
        event_name: EventType member name (e.g. ``"PLUGIN_RUN_START"``).
        payload: Event-specific data dict.
        correlation_id: Optional correlation ID to group related events.
            When empty, the Event dataclass generates a new UUID.
    """
    if event_bus is None:
        return
    kwargs: dict[str, Any] = {"event_type": EventType[event_name], "payload": payload}
    if correlation_id:
        kwargs["correlation_id"] = correlation_id
    await event_bus.emit(Event(**kwargs))


class EventEmitterAdapter:
    """Exposes EventBus to plugins as EventEmitter protocol."""

    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Emit an event through the EventBus.

        Args:
            event_type: EventType member name (e.g. ``"PLUGIN_RUN_COMPLETE"``).
            payload: Event-specific data dict.
        """
        await emit_event(self._bus, event_type, payload)


class EventBus:
    """Central pub/sub bus for BSage component events.

    Components emit events via ``emit()``.  Subscribers (e.g.
    ``WebSocketEventBroadcaster``) receive them via ``on_event()``.
    Subscriber failures are logged but never propagated.
    """

    def __init__(self) -> None:
        self._subscribers: list[EventSubscriber] = []

    def subscribe(self, subscriber: EventSubscriber) -> None:
        """Register a subscriber to receive events."""
        self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        """Remove a subscriber."""
        self._subscribers.remove(subscriber)

    async def emit(self, event: Event) -> None:
        """Dispatch an event to all subscribers.

        Failures in individual subscribers are logged but never propagated.
        """
        for sub in self._subscribers:
            try:
                await sub.on_event(event)
            except Exception:
                logger.warning(
                    "event_subscriber_failed",
                    subscriber=type(sub).__name__,
                    event_type=event.event_type.value,
                    exc_info=True,
                )
