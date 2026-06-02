"""Renderer tests — GPT ConversationDto → markdown with YAML frontmatter."""

from __future__ import annotations

import yaml

from plugin.gpt.parser import ConversationDto, MessageDto
from plugin.gpt.renderer import render_frontmatter_only, render_markdown


def _convo(**overrides):
    base = ConversationDto(
        uuid="conv-abc-123",
        title="Marketing brainstorm",
        created_at="2026-04-12T10:30:00Z",
        updated_at="2026-04-12T10:45:00Z",
        messages=[
            MessageDto(sender="user", text="Help me think about marketing"),
            MessageDto(sender="assistant", text="Sure! Let me help."),
        ],
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TestRenderMarkdown:
    def test_renders_frontmatter_block(self):
        md = render_markdown(_convo())
        assert md.startswith("---\n")
        head, _, _ = md.partition("\n---\n")
        yaml_block = head.removeprefix("---\n")
        meta = yaml.safe_load(yaml_block)
        assert meta["conversation_id"] == "conv-abc-123"
        assert meta["title"] == "Marketing brainstorm"
        assert meta["created_at"] == "2026-04-12T10:30:00Z"
        assert meta["updated_at"] == "2026-04-12T10:45:00Z"
        assert meta["message_count"] == 2
        assert meta["source"] == "chatgpt.com"

    def test_title_h1_present(self):
        md = render_markdown(_convo())
        assert "# Marketing brainstorm" in md

    def test_user_and_assistant_headings(self):
        md = render_markdown(_convo())
        assert "## User" in md
        assert "## Assistant" in md
        assert "Help me think about marketing" in md
        assert "Sure! Let me help." in md

    def test_tool_heading_with_marker(self):
        convo = _convo()
        convo.messages.append(MessageDto(sender="tool", text="[web search]"))
        md = render_markdown(convo)
        assert "## Tool" in md
        assert "<!-- tool call -->" in md
        assert "[web search]" in md

    def test_other_heading_for_unknown_sender(self):
        convo = _convo()
        convo.messages.append(MessageDto(sender="other", text="moderator note"))
        md = render_markdown(convo)
        assert "## Other" in md
        assert "moderator note" in md

    def test_attachments_rendered_as_markers(self):
        convo = _convo()
        convo.messages.append(
            MessageDto(
                sender="assistant",
                text="Here you go",
                attachments=["file-service://img-001", "file-service://doc-002"],
            )
        )
        md = render_markdown(convo)
        assert "<!-- attachment: file-service://img-001 -->" in md
        assert "<!-- attachment: file-service://doc-002 -->" in md

    def test_message_with_only_attachments_renders(self):
        # No text body, but the attachment marker still appears.
        convo = _convo()
        convo.messages.append(MessageDto(sender="assistant", text="", attachments=["image"]))
        md = render_markdown(convo)
        assert "<!-- attachment: image -->" in md

    def test_ends_with_single_newline(self):
        md = render_markdown(_convo())
        assert md.endswith("\n")
        assert not md.endswith("\n\n\n")

    def test_uuid_and_timestamps_appear_in_header(self):
        md = render_markdown(_convo())
        assert "conv-abc-123" in md
        assert "2026-04-12T10:30:00Z" in md
        assert "2026-04-12T10:45:00Z" in md

    def test_handles_none_timestamps(self):
        convo = _convo(created_at=None, updated_at=None)
        md = render_markdown(convo)
        # No crash, backticks still present.
        assert "``" in md or "` `" in md


class TestRenderFrontmatterOnly:
    def test_returns_metadata_dict(self):
        meta = render_frontmatter_only(_convo())
        assert meta["conversation_id"] == "conv-abc-123"
        assert meta["source"] == "chatgpt.com"
        assert meta["message_count"] == 2
