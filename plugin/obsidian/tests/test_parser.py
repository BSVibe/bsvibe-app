"""Frontmatter parser unit tests."""

from __future__ import annotations

from plugin.obsidian.parser import parse_note


class TestParseNote:
    def test_parses_yaml_frontmatter_and_body(self):
        text = "---\ntitle: Hello\ntags:\n  - a\n  - b\n---\nThis is the body.\n"
        meta, body = parse_note(text)
        assert meta == {"title": "Hello", "tags": ["a", "b"]}
        assert body == "This is the body.\n"

    def test_no_frontmatter_returns_empty_metadata(self):
        text = "Just a plain note\nwith two lines.\n"
        meta, body = parse_note(text)
        assert meta == {}
        assert body == "Just a plain note\nwith two lines.\n"

    def test_empty_frontmatter_block(self):
        text = "---\n---\nbody only\n"
        meta, body = parse_note(text)
        assert meta == {}
        assert body == "body only\n"

    def test_unterminated_frontmatter_treated_as_body(self):
        text = "---\ntitle: oops\nstill no closer\n"
        meta, body = parse_note(text)
        # Defensive: invalid frontmatter must not blow up — return as body.
        assert meta == {}
        assert body == text

    def test_non_dict_yaml_in_frontmatter_is_discarded(self):
        # `--- ["a", "b"] ---` — YAML parses but isn't a mapping. Drop it
        # rather than mis-coerce.
        text = "---\n- a\n- b\n---\nbody\n"
        meta, body = parse_note(text)
        assert meta == {}
        assert body == "body\n"

    def test_malformed_yaml_recovered_as_body(self):
        text = "---\n: : :\n---\nbody\n"
        meta, body = parse_note(text)
        # YAML parser may raise — must be caught + treat as no-frontmatter.
        assert meta == {}
        # Falls back to full text as body (caller didn't lose data).
        assert "body" in body

    def test_crlf_line_endings(self):
        text = "---\r\ntitle: x\r\n---\r\nbody\r\n"
        meta, body = parse_note(text)
        assert meta == {"title": "x"}
        assert "body" in body
