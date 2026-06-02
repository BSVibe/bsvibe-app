"""Tests for the Notion block → markdown converter.

The converter MUST handle the 8 MVP block types from the spec and degrade
gracefully (placeholder + log) on unknown types. Rich-text formatting
(bold/italic/code/link) is preserved inline.
"""

from __future__ import annotations

from typing import Any

import pytest

from plugin.notion.converter import (
    extract_page_title,
    render_blocks,
    rich_text_to_markdown,
)


def _text(content: str, **annotations: Any) -> dict[str, Any]:
    """Build a Notion rich_text item with optional annotations."""
    ann = {
        "bold": False,
        "italic": False,
        "strikethrough": False,
        "underline": False,
        "code": False,
        "color": "default",
    }
    ann.update(annotations)
    return {
        "type": "text",
        "text": {"content": content, "link": None},
        "annotations": ann,
        "plain_text": content,
        "href": None,
    }


def _link(content: str, url: str) -> dict[str, Any]:
    item = _text(content)
    item["text"]["link"] = {"url": url}
    item["href"] = url
    return item


def _block(block_type: str, **payload: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "object": "block",
        "id": "blk-1",
        "type": block_type,
        "has_children": False,
    }
    base[block_type] = payload
    return base


class TestRichTextToMarkdown:
    def test_plain_text(self):
        assert rich_text_to_markdown([_text("hello")]) == "hello"

    def test_empty(self):
        assert rich_text_to_markdown([]) == ""
        assert rich_text_to_markdown(None) == ""  # type: ignore[arg-type]

    def test_bold(self):
        assert rich_text_to_markdown([_text("hi", bold=True)]) == "**hi**"

    def test_italic(self):
        assert rich_text_to_markdown([_text("hi", italic=True)]) == "*hi*"

    def test_inline_code(self):
        assert rich_text_to_markdown([_text("x", code=True)]) == "`x`"

    def test_link(self):
        assert rich_text_to_markdown([_link("docs", "https://x.com")]) == "[docs](https://x.com)"

    def test_bold_and_italic_nested(self):
        # bold+italic → ***text***
        item = _text("bi", bold=True, italic=True)
        out = rich_text_to_markdown([item])
        assert out == "***bi***"

    def test_concatenates_segments(self):
        out = rich_text_to_markdown([_text("a"), _text("b", bold=True), _text("c")])
        assert out == "a**b**c"


class TestRenderBlocks:
    def test_paragraph(self):
        blocks = [_block("paragraph", rich_text=[_text("hi")])]
        assert render_blocks(blocks).strip() == "hi"

    def test_heading_1_2_3(self):
        blocks = [
            _block("heading_1", rich_text=[_text("H1")]),
            _block("heading_2", rich_text=[_text("H2")]),
            _block("heading_3", rich_text=[_text("H3")]),
        ]
        md = render_blocks(blocks)
        assert "# H1" in md
        assert "## H2" in md
        assert "### H3" in md

    def test_bulleted_list_item(self):
        blocks = [
            _block("bulleted_list_item", rich_text=[_text("one")]),
            _block("bulleted_list_item", rich_text=[_text("two")]),
        ]
        md = render_blocks(blocks)
        assert "- one" in md
        assert "- two" in md

    def test_numbered_list_item(self):
        blocks = [_block("numbered_list_item", rich_text=[_text("first")])]
        assert "1. first" in render_blocks(blocks)

    def test_code_block_with_language(self):
        blocks = [
            _block(
                "code",
                rich_text=[_text("print('hi')")],
                language="python",
            )
        ]
        md = render_blocks(blocks)
        assert "```python" in md
        assert "print('hi')" in md
        assert md.strip().endswith("```")

    def test_quote(self):
        blocks = [_block("quote", rich_text=[_text("yo")])]
        assert "> yo" in render_blocks(blocks)

    def test_to_do_unchecked_and_checked(self):
        blocks = [
            _block("to_do", rich_text=[_text("buy milk")], checked=False),
            _block("to_do", rich_text=[_text("done thing")], checked=True),
        ]
        md = render_blocks(blocks)
        assert "- [ ] buy milk" in md
        assert "- [x] done thing" in md

    def test_divider(self):
        blocks = [_block("divider")]
        assert "---" in render_blocks(blocks)

    def test_child_page_marker(self):
        blocks = [_block("child_page", title="Sub Page")]
        md = render_blocks(blocks)
        # Linked: rather than recursing — out of scope MVP
        assert "Linked: Sub Page" in md

    def test_image_with_url(self):
        blocks = [
            _block(
                "image",
                type="external",
                external={"url": "https://x.com/a.png"},
            )
        ]
        md = render_blocks(blocks)
        assert "![" in md
        assert "https://x.com/a.png" in md

    def test_unknown_block_placeholder(self):
        blocks = [_block("table_of_contents")]
        md = render_blocks(blocks)
        assert "<!-- unsupported: table_of_contents -->" in md

    def test_eight_block_types_round_trip(self):
        """Spec asserts 8 MVP block types — round-trip a doc with all 8."""
        blocks = [
            _block("heading_1", rich_text=[_text("Title")]),
            _block("paragraph", rich_text=[_text("intro")]),
            _block("heading_2", rich_text=[_text("Sub")]),
            _block("bulleted_list_item", rich_text=[_text("a")]),
            _block("numbered_list_item", rich_text=[_text("one")]),
            _block("to_do", rich_text=[_text("task")], checked=False),
            _block("quote", rich_text=[_text("wisdom")]),
            _block("divider"),
            _block("code", rich_text=[_text("x=1")], language="python"),
        ]
        md = render_blocks(blocks)
        for snippet in [
            "# Title",
            "intro",
            "## Sub",
            "- a",
            "1. one",
            "- [ ] task",
            "> wisdom",
            "---",
            "```python",
            "x=1",
        ]:
            assert snippet in md

    def test_nested_children_are_rendered(self):
        """has_children + children list → render children indented under parent."""
        child = _block("bulleted_list_item", rich_text=[_text("nested")])
        parent = _block("bulleted_list_item", rich_text=[_text("top")])
        parent["has_children"] = True
        parent["children"] = [child]
        md = render_blocks([parent])
        assert "- top" in md
        # Nested item should appear with indentation
        assert "  - nested" in md


class TestExtractPageTitle:
    def test_extracts_title_from_properties_title(self):
        page = {
            "id": "p1",
            "properties": {
                "title": {
                    "id": "title",
                    "type": "title",
                    "title": [_text("My Page")],
                }
            },
        }
        assert extract_page_title(page) == "My Page"

    def test_extracts_title_from_Name_property(self):
        # Database pages name the title property "Name" by convention.
        page = {
            "id": "p1",
            "properties": {
                "Name": {
                    "id": "title",
                    "type": "title",
                    "title": [_text("Hi"), _text(" Friend")],
                }
            },
        }
        assert extract_page_title(page) == "Hi Friend"

    def test_no_title_returns_empty(self):
        assert extract_page_title({"id": "p", "properties": {}}) == ""
        assert extract_page_title({}) == ""

    def test_defensive_against_missing_keys(self):
        page = {"properties": {"title": {"type": "title"}}}
        # title key missing → empty
        assert extract_page_title(page) == ""

    def test_skips_non_title_type_property(self):
        # A property typed "rich_text" must NOT be returned as title.
        page = {
            "properties": {
                "Description": {
                    "type": "rich_text",
                    "rich_text": [_text("text")],
                }
            }
        }
        assert extract_page_title(page) == ""


@pytest.mark.parametrize(
    "annotation,expected",
    [
        ({"bold": True}, "**x**"),
        ({"italic": True}, "*x*"),
        ({"code": True}, "`x`"),
        ({"strikethrough": True}, "~~x~~"),
    ],
)
def test_rich_text_annotations(annotation, expected):
    assert rich_text_to_markdown([_text("x", **annotation)]) == expected
