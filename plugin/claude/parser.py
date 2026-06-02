"""Parser for Claude's ``conversations.json`` export (Lift Q3-Claude).

The exported file is an array of conversation objects; each carries a
``chat_messages`` list with ``sender`` ("human" / "assistant" / sometimes
"tool" / "system") and a ``text`` payload that may be either a plain
string OR an Anthropic content-block array (the schema has evolved).

This parser is *defensive*: every conversation missing a stable id or
its message list is dropped with a structured warning and counted under
``skipped`` so a single bad entry never aborts the import batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class MessageDto:
    """One chat message, normalised to ``(sender, text, created_at)``."""

    sender: str
    text: str
    created_at: str | None = None


@dataclass
class ConversationDto:
    """One conversation, ready for rendering."""

    uuid: str
    title: str
    created_at: str | None
    updated_at: str | None
    messages: list[MessageDto] = field(default_factory=list)


# ── message-text normalisation ────────────────────────────────────────────


def _flatten_text(raw: Any) -> str:
    """Coerce a message's ``text`` field into a plain string.

    Anthropic's export historically used a string but newer exports
    sometimes carry an array of content blocks (``{"type": "text",
    "text": "..."}``). Anything else (None, dict, number) returns the
    empty string — the renderer will treat that message as a no-op.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        chunks: list[str] = []
        for block in raw:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                # Anthropic content-block shape.
                value = block.get("text")
                if isinstance(value, str):
                    chunks.append(value)
        return "\n".join(c for c in chunks if c)
    return ""


def _normalise_sender(raw: Any) -> str:
    """Map a sender label to one of ``human``/``assistant``/``other``."""
    if not isinstance(raw, str):
        return "other"
    lowered = raw.lower().strip()
    if lowered in ("human", "user"):
        return "human"
    if lowered in ("assistant", "ai"):
        return "assistant"
    return "other"


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp; tolerate trailing ``Z`` and None.

    Always returns a timezone-aware datetime — naive inputs (e.g. a
    date-only ``2026-04-18`` cutoff) are assumed UTC so ``since``
    comparisons against tz-aware export timestamps don't blow up.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


# ── public surface ────────────────────────────────────────────────────────


def parse_conversation(raw: dict[str, Any]) -> ConversationDto | None:
    """Convert one raw conversation dict into a :class:`ConversationDto`.

    Returns ``None`` and logs a warning when the entry is missing
    structural fields (uuid / chat_messages) so callers can count it
    under ``skipped``.
    """
    if not isinstance(raw, dict):
        logger.warning("claude_conversation_not_a_dict")
        return None

    uuid = raw.get("uuid") or raw.get("id")
    if not isinstance(uuid, str) or not uuid:
        logger.warning("claude_conversation_missing_uuid")
        return None

    chat_messages = raw.get("chat_messages")
    if not isinstance(chat_messages, list):
        logger.warning("claude_conversation_missing_messages", uuid=uuid)
        return None

    title = raw.get("name") or raw.get("title") or "Untitled"
    if not isinstance(title, str):
        title = str(title)

    created_at = raw.get("created_at") if isinstance(raw.get("created_at"), str) else None
    updated_at = raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None

    messages: list[MessageDto] = []
    for raw_msg in chat_messages:
        if not isinstance(raw_msg, dict):
            continue
        sender = _normalise_sender(raw_msg.get("sender"))
        text = _flatten_text(raw_msg.get("text"))
        if not text:
            # Skip messages with empty bodies — they don't carry signal.
            continue
        msg_created = (
            raw_msg.get("created_at") if isinstance(raw_msg.get("created_at"), str) else None
        )
        messages.append(MessageDto(sender=sender, text=text, created_at=msg_created))

    return ConversationDto(
        uuid=uuid,
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        messages=messages,
    )


def parse_export(payload: Any, since: str | None = None) -> tuple[list[ConversationDto], int]:
    """Parse a full ``conversations.json`` payload.

    Returns ``(conversations, skipped_count)`` where ``conversations`` is
    every entry that survived parse + filter and ``skipped_count`` is the
    number of entries that were either malformed, had zero messages, or
    fell below the ``since`` cutoff.
    """
    if not isinstance(payload, list):
        logger.warning("claude_export_not_a_list", got=type(payload).__name__)
        return [], 0

    cutoff = _parse_iso(since) if since else None

    conversations: list[ConversationDto] = []
    skipped = 0
    for entry in payload:
        convo = parse_conversation(entry)
        if convo is None:
            skipped += 1
            continue
        if not convo.messages:
            # Spec: skip conversations with zero messages.
            logger.warning("claude_conversation_empty", uuid=convo.uuid)
            skipped += 1
            continue
        if cutoff is not None:
            updated = _parse_iso(convo.updated_at)
            if updated is None or updated < cutoff:
                skipped += 1
                continue
        conversations.append(convo)

    return conversations, skipped


__all__ = ["ConversationDto", "MessageDto", "parse_conversation", "parse_export"]
