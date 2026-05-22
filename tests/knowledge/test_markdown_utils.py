"""Tests for backend.knowledge.graph.markdown_utils — shared markdown parsing utilities."""

from backend.knowledge.graph.markdown_utils import (
    body_after_frontmatter,
    extract_frontmatter,
    extract_title,
)


class TestExtractFrontmatter:
    def test_valid_frontmatter(self) -> None:
        text = "---\ntitle: Hello\ntype: idea\n---\nBody"
        fm = extract_frontmatter(text)
        assert fm["title"] == "Hello"
        assert fm["type"] == "idea"

    def test_no_frontmatter(self) -> None:
        assert extract_frontmatter("No frontmatter here") == {}

    def test_no_closing_delimiter(self) -> None:
        assert extract_frontmatter("---\ntitle: Hello\nBody") == {}

    def test_malformed_yaml(self) -> None:
        text = "---\n: bad: yaml:\n---\n"
        result = extract_frontmatter(text)
        assert isinstance(result, dict)

    def test_non_dict_yaml(self) -> None:
        text = "---\n- list item\n---\nBody"
        assert extract_frontmatter(text) == {}

    def test_empty_string(self) -> None:
        assert extract_frontmatter("") == {}


class TestExtractTitle:
    def test_h1_heading(self) -> None:
        assert extract_title("# My Title\n\nBody") == "My Title"

    def test_no_heading(self) -> None:
        assert extract_title("No heading here") == ""

    def test_ignores_h2(self) -> None:
        assert extract_title("## Not H1\n\nBody") == ""

    def test_with_frontmatter(self) -> None:
        text = "---\ntitle: FM\n---\n# Heading Title\nBody"
        assert extract_title(text) == "Heading Title"

    def test_empty_string(self) -> None:
        assert extract_title("") == ""


class TestBodyAfterFrontmatter:
    def test_with_frontmatter(self) -> None:
        text = "---\ntitle: Hello\n---\nBody text"
        assert body_after_frontmatter(text) == "Body text"

    def test_without_frontmatter(self) -> None:
        text = "Just body text"
        assert body_after_frontmatter(text) == "Just body text"

    def test_no_closing_delimiter(self) -> None:
        text = "---\ntitle: Hello\nNo close"
        assert body_after_frontmatter(text) == text

    def test_empty_string(self) -> None:
        assert body_after_frontmatter("") == ""
