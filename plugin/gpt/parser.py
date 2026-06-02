"""Parser for OpenAI ChatGPT ``conversations.json`` export (Lift Q3-GPT).

The exported file is an array of conversation objects; each carries a
``mapping`` graph (``node_id`` → ``{message, parent, children}``) rather
than a flat message list. Messages may be ``None`` (pure structural
nodes), have role ``system`` (skipped — meta instructions), have role
``tool`` (rendered as ``other`` so the renderer emits a ``## Tool``
heading), or be multimodal (text + non-text content parts).

Branched conversations (user edited a turn, multiple assistant branches
exist) are resolved by taking the **first-child path** — the canonical
"current" branch as ChatGPT presents it. We trade lossless graph
preservation for a single linear markdown body. Documented in the PR.

Timestamps are Unix epoch (``int`` or ``float``). They are converted to
ISO-8601 UTC strings so the rendered frontmatter matches the Claude /
Notion / Obsidian shape and the ``since`` filter accepts BOTH numeric
epoch cutoffs and ISO strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class MessageDto:
    """One chat message, normalised to ``(sender, text, created_at)``.

    ``sender`` is one of ``user`` / ``assistant`` / ``tool`` / ``other``
    (system messages are dropped before reaching the renderer).
    ``attachments`` carries placeholder labels for any non-text content
    parts encountered in a ``multimodal_text`` message (image asset
    pointers, file references, etc.).
    """

    sender: str
    text: str
    created_at: str | None = None
    attachments: list[str] = field(default_factory=list)


@dataclass
class ConversationDto:
    """One conversation, ready for rendering."""

    uuid: str
    title: str
    created_at: str | None
    updated_at: str | None
    messages: list[MessageDto] = field(default_factory=list)


# ── timestamp + role helpers ──────────────────────────────────────────────


def _epoch_to_iso(value: Any) -> str | None:
    """Convert a Unix epoch ``int`` / ``float`` to an ISO-8601 UTC string.

    Returns ``None`` when the input is missing or non-numeric. Tolerates
    sub-second floats (ChatGPT exports include microseconds).
    """
    if isinstance(value, bool):
        # bool is a subclass of int — guard explicitly.
        return None
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat().replace("+00:00", "Z")
    except (OSError, ValueError, OverflowError):
        return None


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 string to a tz-aware UTC datetime.

    Naive inputs (date-only ``2026-04-18``) are assumed UTC so ``since``
    comparisons against the converted epoch timestamps don't blow up.
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


def _coerce_since_to_datetime(value: Any) -> datetime | None:
    """Accept Unix epoch (int/float) OR ISO string for the ``since`` cutoff."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        return _parse_iso(value)
    return None


def _normalise_role(raw: Any) -> str:
    """Map a ChatGPT author role to the parser-internal sender label."""
    if not isinstance(raw, str):
        return "other"
    lowered = raw.lower().strip()
    if lowered == "user":
        return "user"
    if lowered == "assistant":
        return "assistant"
    if lowered in {"tool", "function"}:
        return "tool"
    if lowered == "system":
        return "system"
    return "other"


def _flatten_parts(parts: Any) -> tuple[str, list[str]]:
    """Coerce ``content.parts`` into ``(text, attachment_labels)``.

    Text parts are joined with ``\\n``; non-text parts (dicts describing
    image asset pointers, file references, etc.) contribute a label to
    the attachments list. Anything that can't be classified is ignored
    so the renderer never crashes on schema drift.
    """
    if not isinstance(parts, list):
        return "", []
    chunks: list[str] = []
    attachments: list[str] = []
    for part in parts:
        if isinstance(part, str):
            if part:
                chunks.append(part)
        elif isinstance(part, dict):
            ctype = part.get("content_type") or part.get("type")
            if ctype == "image_asset_pointer":
                pointer = part.get("asset_pointer") or "image"
                attachments.append(str(pointer))
            elif ctype in ("audio_asset_pointer", "video_container_asset_pointer"):
                attachments.append(str(part.get("asset_pointer") or ctype))
            elif "text" in part and isinstance(part["text"], str):
                chunks.append(part["text"])
            else:
                # Unknown structural part — record a generic placeholder
                # so the founder sees *something* changed.
                attachments.append(str(ctype or "attachment"))
    return "\n".join(chunks), attachments


# ── graph traversal ──────────────────────────────────────────────────────


def _linearise_mapping(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the ``mapping`` graph and return message nodes in order.

    Strategy: find root nodes (``parent is None``); for each root walk
    the **first-child path** depth-first. This yields the canonical
    "current" branch of any edited conversation while staying simple.
    A ``visited`` set guards against the (theoretical) cyclic export.
    Structural nodes (``message=None``) are skipped but their children
    are still walked.
    """
    roots: list[str] = [
        nid
        for nid, node in mapping.items()
        if isinstance(node, dict) and node.get("parent") is None
    ]
    if not roots:
        # Some exports omit the synthetic root — fall back to "any node
        # whose parent isn't itself a key in the mapping" to recover.
        roots = [
            nid
            for nid, node in mapping.items()
            if isinstance(node, dict) and node.get("parent") not in mapping
        ]

    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()
    for root in roots:
        cursor: str | None = root
        while cursor is not None and cursor not in visited:
            visited.add(cursor)
            node = mapping.get(cursor)
            if not isinstance(node, dict):
                break
            msg = node.get("message")
            if isinstance(msg, dict):
                ordered.append(msg)
            children = node.get("children")
            if isinstance(children, list) and children:
                cursor = children[0] if isinstance(children[0], str) else None
            else:
                cursor = None
    return ordered


# ── public surface ────────────────────────────────────────────────────────


def parse_conversation(raw: Any) -> ConversationDto | None:
    """Convert one raw conversation dict into a :class:`ConversationDto`.

    Returns ``None`` (and logs a structured warning) when the entry is
    missing structural fields (id / mapping) so the caller can count
    it under ``skipped``.
    """
    if not isinstance(raw, dict):
        logger.warning("gpt_conversation_not_a_dict")
        return None

    conversation_id = raw.get("id") or raw.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        logger.warning("gpt_conversation_missing_id")
        return None

    mapping = raw.get("mapping")
    if not isinstance(mapping, dict):
        logger.warning("gpt_conversation_missing_mapping", id=conversation_id)
        return None

    title = raw.get("title") or "Untitled"
    if not isinstance(title, str):
        title = str(title)

    created_at = _epoch_to_iso(raw.get("create_time"))
    updated_at = _epoch_to_iso(raw.get("update_time"))

    messages: list[MessageDto] = []
    for raw_msg in _linearise_mapping(mapping):
        author = raw_msg.get("author")
        role = _normalise_role(author.get("role") if isinstance(author, dict) else None)
        if role == "system":
            # Skip system meta-instructions — they don't carry user signal.
            continue
        content = raw_msg.get("content")
        if not isinstance(content, dict):
            continue
        text, attachments = _flatten_parts(content.get("parts"))
        if not text and not attachments:
            # Nothing of substance in this message — drop it.
            continue
        msg_created = _epoch_to_iso(raw_msg.get("create_time"))
        messages.append(
            MessageDto(
                sender=role,
                text=text,
                created_at=msg_created,
                attachments=attachments,
            )
        )

    return ConversationDto(
        uuid=conversation_id,
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        messages=messages,
    )


def parse_export(
    payload: Any, since: str | int | float | None = None
) -> tuple[list[ConversationDto], int]:
    """Parse a full ``conversations.json`` payload.

    Returns ``(conversations, skipped_count)``. ``skipped_count``
    accumulates every entry that was malformed, had zero messages, or
    fell below the ``since`` cutoff. ``since`` accepts an ISO-8601
    string OR a Unix epoch number — the export uses epoch natively, so
    callers can forward either form.
    """
    if not isinstance(payload, list):
        logger.warning("gpt_export_not_a_list", got=type(payload).__name__)
        return [], 0

    cutoff = _coerce_since_to_datetime(since)

    conversations: list[ConversationDto] = []
    skipped = 0
    for entry in payload:
        convo = parse_conversation(entry)
        if convo is None:
            skipped += 1
            continue
        if not convo.messages:
            logger.warning("gpt_conversation_empty", id=convo.uuid)
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
