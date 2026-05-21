"""GardenWriter — backwards-compat re-export shim.

The original ~900-line module was split into focused submodules
(:mod:`bsage.garden.note`, :mod:`bsage.garden.writer_tools`,
:mod:`bsage.garden.writer_core`) during Hardening Sprint 2 (M15). This file
preserves the historical public surface so existing call sites such as ::

    from backend.knowledge.graph.writer import (
        GardenNote,
        GardenWriter,
        WRITE_NOTE_TOOL,
        ...,
    )

continue to work without modification. New code should prefer importing from
the focused submodules directly.
"""

from __future__ import annotations

from backend.knowledge.graph.note import (
    _MAX_ACTION_SUMMARY,
    GardenNote,
    _build_frontmatter,
    _slugify,
    build_frontmatter,
    slugify,
)
from backend.knowledge.graph.writer_core import GardenWriter
from backend.knowledge.graph.writer_tools import (
    APPEND_NOTE_TOOL,
    DELETE_NOTE_TOOL,
    SEARCH_VAULT_TOOL,
    UPDATE_NOTE_TOOL,
    WRITE_NOTE_TOOL,
    WRITE_SEED_TOOL,
)

__all__ = [
    "APPEND_NOTE_TOOL",
    "DELETE_NOTE_TOOL",
    "GardenNote",
    "GardenWriter",
    "SEARCH_VAULT_TOOL",
    "UPDATE_NOTE_TOOL",
    "WRITE_NOTE_TOOL",
    "WRITE_SEED_TOOL",
    "_MAX_ACTION_SUMMARY",
    "_build_frontmatter",
    "_slugify",
    "build_frontmatter",
    "slugify",
]
