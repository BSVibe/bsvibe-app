"""Markdown renderer for ChatGPT conversation imports (Lift Q3-GPT).

Each :class:`~plugin.gpt.parser.ConversationDto` becomes ONE markdown
document with YAML frontmatter carrying stable provenance fields.
Heading mapping:

* ``user`` → ``## User``
* ``assistant`` → ``## Assistant``
* ``tool`` → ``## Tool`` (with a ``<!-- tool call -->`` marker so the
  classifier can see structurally that this is an agentic invocation)
* anything else → ``## Other``

Multimodal attachments render as ``<!-- attachment: <pointer> -->``
markers after the text body so the markdown still flows naturally for a
human reader and the marker is grep-able for follow-up processing.
"""

from __future__ import annotations

from typing import Any

import yaml

from plugin.gpt.parser import ConversationDto

_SENDER_HEADING = {
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
    "other": "Other",
}


def _frontmatter(convo: ConversationDto) -> dict[str, Any]:
    """Build the YAML frontmatter dict for a conversation."""
    return {
        "conversation_id": convo.uuid,
        "title": convo.title,
        "created_at": convo.created_at,
        "updated_at": convo.updated_at,
        "message_count": len(convo.messages),
        "source": "chatgpt.com",
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
        if msg.sender == "tool":
            # Marker keeps tool invocations grep-able without polluting
            # the founder-readable body.
            lines.append("<!-- tool call -->")
        if msg.text:
            lines.append(msg.text.rstrip())
        for pointer in msg.attachments:
            lines.append(f"<!-- attachment: {pointer} -->")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_frontmatter_only(convo: ConversationDto) -> dict[str, Any]:
    """Expose the structured frontmatter — handy for write_seed metadata."""
    return _frontmatter(convo)


__all__ = ["render_markdown", "render_frontmatter_only"]
