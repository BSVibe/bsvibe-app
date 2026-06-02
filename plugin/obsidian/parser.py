"""Tiny YAML-frontmatter parser for Obsidian notes.

Standalone (no external ``python-frontmatter`` dep — the project already
ships PyYAML which is the only thing we actually need). Returns
``(metadata, body)``; missing / malformed frontmatter degrades to
``({}, full_text)`` so a bad note never breaks an import batch.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

# ``--- ... ---`` block at the very start of the file. ``DOTALL`` so the
# YAML payload may span multiple lines. Tolerant of CRLF line endings.
_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(?P<yaml>.*?)\r?\n?---\r?\n?(?P<body>.*)\Z",
    re.DOTALL,
)


def parse_note(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter at the top of an Obsidian note.

    Returns ``(metadata, body)``. When no valid frontmatter block is
    present (or YAML parses to a non-mapping, or the block is unterminated,
    or the YAML is malformed) ``metadata`` is empty and ``body`` is the
    original text unchanged — the caller never loses data.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text

    yaml_block = match.group("yaml")
    body = match.group("body")

    if not yaml_block.strip():
        return {}, body

    try:
        loaded = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        # Malformed YAML — preserve the whole file as body and move on.
        return {}, text

    if not isinstance(loaded, dict):
        # YAML parses to a list / scalar / None — not metadata. Drop it
        # rather than mis-coerce.
        return {}, body

    return loaded, body


__all__ = ["parse_note"]
