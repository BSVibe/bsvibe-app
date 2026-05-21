"""Shared markdown parsing utilities for vault notes."""

from __future__ import annotations

from typing import Any

import yaml


def extract_frontmatter(text: str) -> dict[str, Any]:
    """Extract YAML frontmatter from note text.

    Returns an empty dict if the text has no valid frontmatter block.
    """
    if not text.startswith("---\n"):
        return {}
    try:
        end_idx = text.index("\n---\n", 4)
        fm = yaml.safe_load(text[4:end_idx])
        return fm if isinstance(fm, dict) else {}
    except (ValueError, yaml.YAMLError):
        return {}


def extract_title(text: str) -> str:
    """Extract title from the first ``# `` heading, or return empty string."""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def body_after_frontmatter(text: str) -> str:
    """Return the body text after the YAML frontmatter block."""
    if not text.startswith("---\n"):
        return text
    try:
        end_idx = text.index("\n---\n", 4)
        return text[end_idx + 5 :]
    except ValueError:
        return text
