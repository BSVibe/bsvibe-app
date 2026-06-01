"""GardenWriter — writes structured markdown notes to the vault.

Lift L1 (v8 §17.3) decomposed the original 1159-LOC monolithic
``writer_core.py`` into a package. The public import path
``from backend.knowledge.graph.writer_core import GardenWriter`` is
preserved — every public symbol the old module exposed is re-exported
here.

Sub-modules:

* :mod:`_io` — :class:`_WriterIOMixin` (seed / garden / action / read).
* :mod:`_mutation` — :class:`_WriterMutationMixin` (update / promote / delete).
* :mod:`_tool_handlers` — :class:`_WriterToolHandlersMixin` (LLM tool-call adapters).
* :mod:`_entity_stub` — pure helpers for entity-stub + frontmatter split/merge.
* :mod:`_core` — :class:`GardenWriter` composed public class.

``bsage.garden.writer`` (the legacy facade) imports :class:`GardenWriter`
from this package, so existing call sites keep working unchanged.
"""

from __future__ import annotations

# Re-export public + previously-importable internal symbols so call sites
# and tests that did ``from backend.knowledge.graph.writer_core import X``
# keep working without any source change.
from backend.knowledge.graph.note import GardenNote
from backend.knowledge.graph.writer_core._core import GardenWriter
from backend.knowledge.graph.writer_core._entity_stub import (
    _create_entity_stub,
    _maturity_from_status,
    _rewrite_mentioned_in_section,
    _split_frontmatter,
    _update_entity_stub_mentions,
)
from backend.knowledge.graph.writer_core._io import _WriterIOMixin
from backend.knowledge.graph.writer_core._mutation import _WriterMutationMixin
from backend.knowledge.graph.writer_core._tool_handlers import _WriterToolHandlersMixin

__all__ = [
    "GardenNote",
    "GardenWriter",
    "_WriterIOMixin",
    "_WriterMutationMixin",
    "_WriterToolHandlersMixin",
    "_create_entity_stub",
    "_maturity_from_status",
    "_rewrite_mentioned_in_section",
    "_split_frontmatter",
    "_update_entity_stub_mentions",
]
