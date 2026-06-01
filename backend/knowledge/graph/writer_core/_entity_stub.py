"""Module-level helpers for entity-stub frontmatter + mentions section rewriting.

Extracted from the original ``writer_core.py`` during Lift L1
(v8 §17.3). These helpers are intentionally pure functions (no class state)
so the IO mixin can call them under its existing ``_garden_lock``.

Keep this file pure-helpers — anything that needs vault/sync/event_bus state
belongs in ``_io.py`` or ``_mutation.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from backend.knowledge.graph.note import build_frontmatter


def _maturity_from_status(status: str) -> str:
    """Map a legacy ``status:`` value onto the maturity vocabulary.

    Pre-refactor notes wrote ``status: seed | growing | evergreen``. The
    new layout uses ``maturity: seedling | budding | evergreen``. The
    promote_maturity migration writes both fields; this helper covers
    pre-migration reads so the comparison "did the maturity change?"
    works on legacy notes.
    """
    mapping = {
        "seed": "seedling",
        "seedling": "seedling",
        "growing": "budding",
        "budding": "budding",
        "evergreen": "evergreen",
    }
    return mapping.get(status.strip().lower(), "seedling")


def _create_entity_stub(file_path: Path, name: str, first_mention: str) -> None:
    """Write a brand-new auto-stub file for ``[[name]]``."""
    now_iso = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    metadata = {
        "title": name,
        "created": now_iso,
        "maturity": "seedling",
        "auto_stub": True,
        "mentions": [first_mention],
    }
    body = (
        f"\n# {name}\n\n"
        f"> Auto-generated entity stub for [[{name}]]. "
        "Filled in as it gets mentioned.\n\n"
        "## Mentioned in\n\n"
        f"- [[{Path(first_mention).stem}]]\n"
    )
    file_path.write_text(build_frontmatter(metadata) + body, encoding="utf-8")


def _update_entity_stub_mentions(file_path: Path, mention: str) -> None:
    """Append ``mention`` to an existing entity stub idempotently.

    Touches frontmatter ``mentions:`` always; rewrites the ``## Mentioned in``
    section only when the stub is still an auto-stub (the user hasn't taken
    it over yet).
    """
    raw = file_path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(raw)
    mentions = list(fm.get("mentions") or [])
    if mention in mentions:
        return
    mentions.append(mention)
    fm["mentions"] = mentions

    is_auto_stub = bool(fm.get("auto_stub"))
    if is_auto_stub:
        body = _rewrite_mentioned_in_section(body, mentions)

    file_path.write_text(build_frontmatter(fm) + body, encoding="utf-8")


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file with leading ``---`` frontmatter into (dict, body)."""
    if not raw.startswith("---\n"):
        return {}, raw
    closing = raw.find("\n---\n", 4)
    if closing == -1:
        return {}, raw
    fm_text = raw[4:closing]
    body = raw[closing + 5 :]
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return {}, raw
    if not isinstance(fm, dict):
        return {}, raw
    return fm, body


def _rewrite_mentioned_in_section(body: str, mentions: list[str]) -> str:
    """Replace the ``## Mentioned in`` block with the latest mentions list."""
    marker = "## Mentioned in"
    items = "\n".join(f"- [[{Path(m).stem}]]" for m in mentions)
    new_section = f"{marker}\n\n{items}\n"
    idx = body.find(marker)
    if idx == -1:
        # Stub had no section yet — append one.
        return body.rstrip() + "\n\n" + new_section
    # Cut from the marker through the next H2 (or end of file).
    after = body.find("\n## ", idx + len(marker))
    if after == -1:
        return body[:idx] + new_section
    return body[:idx] + new_section + body[after:]
