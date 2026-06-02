"""Renderer tests — ConversationDto → markdown with YAML frontmatter."""

from __future__ import annotations

import yaml

from plugin.claude.parser import ConversationDto, MessageDto
from plugin.claude.renderer import render_frontmatter_only, render_markdown


def _convo(**overrides):
    base = ConversationDto(
        uuid="conv-1",
        title="Marketing brainstorm",
        created_at="2026-04-12T10:30:00Z",
        updated_at="2026-04-12T10:45:00Z",
        messages=[
            MessageDto(sender="human", text="Help me think about marketing"),
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
        # The closing --- is followed by the title block.
        head, _, _ = md.partition("\n---\n")
        # head holds the opening fence + the YAML payload.
        yaml_block = head.removeprefix("---\n")
        meta = yaml.safe_load(yaml_block)
        assert meta["conversation_uuid"] == "conv-1"
        assert meta["title"] == "Marketing brainstorm"
        assert meta["created_at"] == "2026-04-12T10:30:00Z"
        assert meta["updated_at"] == "2026-04-12T10:45:00Z"
        assert meta["message_count"] == 2
        assert meta["source"] == "claude.ai"

    def test_title_h1_present(self):
        md = render_markdown(_convo())
        assert "# Marketing brainstorm" in md

    def test_human_and_assistant_headings(self):
        md = render_markdown(_convo())
        assert "## Human" in md
        assert "## Assistant" in md
        assert "Help me think about marketing" in md
        assert "Sure! Let me help." in md

    def test_unknown_sender_renders_as_other(self):
        convo = _convo()
        convo.messages.append(MessageDto(sender="other", text="tool log"))
        md = render_markdown(convo)
        assert "## Other" in md
        assert "tool log" in md

    def test_ends_with_single_newline(self):
        md = render_markdown(_convo())
        assert md.endswith("\n")
        assert not md.endswith("\n\n\n")

    def test_uuid_and_timestamps_appear_in_header(self):
        md = render_markdown(_convo())
        assert "conv-1" in md
        assert "2026-04-12T10:30:00Z" in md
        assert "2026-04-12T10:45:00Z" in md

    def test_handles_none_timestamps(self):
        convo = _convo(created_at=None, updated_at=None)
        md = render_markdown(convo)
        # Backticks remain even when timestamps are absent — no crash.
        assert "``" in md or "` `" in md


class TestRenderFrontmatterOnly:
    def test_returns_metadata_dict(self):
        meta = render_frontmatter_only(_convo())
        assert meta["conversation_uuid"] == "conv-1"
        assert meta["source"] == "claude.ai"
        assert meta["message_count"] == 2
