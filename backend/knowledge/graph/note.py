"""GardenNote dataclass and frontmatter / slug helpers.

Split out of ``bsage/garden/writer.py`` (M15, Hardening Sprint 2) so that the
pure-data layer can be imported without dragging the full ``GardenWriter``
implementation. ``writer.py`` re-exports these symbols for backwards
compatibility — existing call sites keep working unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import yaml

_MAX_ACTION_SUMMARY: int = 200
"""Cap on action-log summary length before truncation with an ellipsis."""


@dataclass
class GardenNote:
    """Structured representation of a garden note.

    Identity in this graph comes from what a note connects to (entities,
    tags, community membership), not from a fixed ``note_type`` enum.
    ``note_type`` is preserved as a deprecated optional field for one
    minor cycle so existing vaults read back without crashing — new
    writes leave it ``None`` and let tags + entities + maturity carry
    the meaning instead.

    Attributes:
        title: Human-readable title for the note.
        content: Markdown body content.
        source: Name of the skill or source that created this note.
        note_type: DEPRECATED. Optional kind tag preserved for back-compat
            with vaults from before the dynamic-ontology refactor; new
            writes leave it ``None``.
        maturity: Andy Matuschak-style growth stage —
            ``seedling`` (just captured), ``budding`` (in progress), or
            ``evergreen`` (curated). Drives the vault folder location
            via :meth:`GardenWriter._resolve_folder`. Defaults to
            ``seedling`` for new captures.
        tags: Free-form lowercase content tags (e.g. "self-hosting",
            "reverse-proxy"). What the note is ABOUT, not what KIND it is.
        entities: Wikilink targets (e.g. "[[Vaultwarden]]") extracted
            from the content. Each must also appear as a wikilink in
            ``content`` — auto-stub creation walks this list.
        related: List of related note titles for ``related:`` field.
        confidence: Content confidence score (0.0-1.0).
        knowledge_layer: Knowledge layer classification.
        relations: Typed relations dict — key is relation type, value is list of targets.
                   Example: {"attendees": ["[[Alice]]"], "belongs_to": ["[[Project X]]"]}
        aliases: Alternative names for Obsidian search.
        extra_fields: Additional frontmatter fields.
    """

    title: str
    content: str
    source: str
    note_type: str | None = None
    maturity: str = "seedling"
    related: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    confidence: float = 0.9
    knowledge_layer: str = "semantic"
    relations: dict[str, list[str]] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    extra_fields: dict[str, Any] = field(default_factory=dict)
    # Phase 0 P0.5 — tenant isolation. None means "use writer default" (set by
    # GardenWriter from settings.default_tenant_id) so cron / local writes
    # don't 500. Authenticated route handlers MUST set this to the active
    # tenant_id from the principal.
    tenant_id: str | None = None


def slugify(title: str) -> str:
    """Convert a title to a URL-friendly slug.

    Lowercase, replace spaces with hyphens, remove special characters.
    Preserves Unicode word characters (Korean, Japanese, Chinese, etc.).
    Falls back to a timestamp when the result would be empty.
    """
    slug = title.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    if not slug:
        slug = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    return slug


def build_frontmatter(metadata: dict) -> str:
    """Build YAML frontmatter block from a dict."""
    dumped = yaml.dump(metadata, default_flow_style=False, allow_unicode=True).strip()
    return f"---\n{dumped}\n---\n"


# ---------------------------------------------------------------------------
# Internal aliases — preserved so `from bsage.garden.writer import _slugify`
# continues to work for any external consumer that imported the private name.
# ---------------------------------------------------------------------------
_slugify = slugify
_build_frontmatter = build_frontmatter

__all__ = [
    "GardenNote",
    "_MAX_ACTION_SUMMARY",
    "_build_frontmatter",
    "_slugify",
    "build_frontmatter",
    "slugify",
]
