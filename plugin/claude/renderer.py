"""Markdown renderer for Claude conversation imports (Lift Q3-Claude).

Each :class:`~plugin.claude.parser.ConversationDto` becomes ONE markdown
document with YAML frontmatter carrying stable provenance fields. The
body alternates ``## Human`` / ``## Assistant`` sections; unknown sender
types render as ``## Other`` so we never lose signal.
"""

from __future__ import annotations

from typing import Any

import yaml

from plugin.claude.parser import ConversationDto

_SENDER_HEADING = {
    "human": "Human",
    "assistant": "Assistant",
    "other": "Other",
}


def _frontmatter(convo: ConversationDto) -> dict[str, Any]:
    """Build the YAML frontmatter dict for a conversation."""
    return {
        "conversation_uuid": convo.uuid,
        "title": convo.title,
        "created_at": convo.created_at,
        "updated_at": convo.updated_at,
        "message_count": len(convo.messages),
        "source": "claude.ai",
    }


def render_markdown(convo: ConversationDto) -> str:
    """Render one conversation as a frontmatter + body markdown document."""
    meta = _frontmatter(convo)
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()

    lines: list[str] = [
        "---",
        fm,
        "---",
        "",
        f"# {convo.title}",
        "",
        f"`{convo.created_at or ''}` · `{convo.updated_at or ''}` · `{convo.uuid}`",
        "",
        "---",
        "",
    ]

    for msg in convo.messages:
        heading = _SENDER_HEADING.get(msg.sender, "Other")
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(msg.text.rstrip())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_frontmatter_only(convo: ConversationDto) -> dict[str, Any]:
    """Expose the structured frontmatter — handy for write_seed metadata."""
    return _frontmatter(convo)


__all__ = ["render_markdown", "render_frontmatter_only"]
