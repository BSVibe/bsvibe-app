"""Merge a plugin's provenance mapping into a seed note's frontmatter.

``write_seed`` accepts ``data["frontmatter"]`` — a mapping of caller-supplied
provenance. The four import plugins have always filled it (notion:
``notion_page_id`` / ``url`` / raw ``properties``; claude + gpt:
``conversation_uuid``, timestamps, message count) and it was dropped on the
floor, because ``write_seed`` only ever read ``title`` / ``tags`` / ``content``.
``render_frontmatter_only`` even documents itself as *"handy for write_seed
metadata"* — the intent was always this seam, the wiring was not.

Its own module rather than more lines in ``_io.py``: that file sits against the
package's 350-LOC cap, and the two rules below (precedence, serializability)
are worth reading without the file IO around them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
import yaml

logger = structlog.get_logger(__name__)


def _is_safe_yaml(value: Any) -> bool:
    """Can ``yaml.safe_load`` read this value back?

    Whatever ``safe_dump`` accepts is what ``safe_load`` can return.
    """
    try:
        yaml.safe_dump(value)
    except yaml.YAMLError:
        return False
    return True


def merge_seed_frontmatter(
    metadata: dict[str, Any],
    extra: Any,
    *,
    source: str,
) -> bool:
    """Fold ``extra`` into ``metadata`` in place. Returns whether it merged.

    ``metadata`` already holds the system fields, so **they win by
    construction** — a plugin must not restate ``source``. claude's mapping
    says ``claude.ai`` while the seed lives under ``seeds/claude/``, and a note
    whose frontmatter disagrees with its own path is worse than one that omits
    the detail.

    Two things are refused, each for a different reason:

    * a non-mapping ``extra`` — the contract is a mapping, and raising here
      would cost the whole item: a seed write that throws is a seed the plugin
      skips entirely, so losing an import to salvage one malformed field is the
      wrong trade.
    * a value ``yaml.safe_load`` could not read back — this one is subtler and
      worse. :func:`build_frontmatter` uses ``yaml.dump``, which does NOT raise
      on an arbitrary object; it writes a ``!!python/object:`` tag. Every reader
      of note frontmatter in this repo uses ``yaml.safe_load``, which DOES raise
      on that tag. The write would succeed and the note would be unreadable from
      then on — silent, and it does not come back. Drop the one key, keep the
      rest of the provenance.

    The caller returns ``False`` to mean "``frontmatter`` was not consumed", so
    the YAML-dump body fallback knows whether it still has to emit it.
    """
    if not isinstance(extra, Mapping):
        if extra is not None:
            logger.warning(
                "seed_frontmatter_not_a_mapping",
                source=source,
                received_type=type(extra).__name__,
            )
        return False

    for key, value in extra.items():
        if key in metadata:
            continue
        if not _is_safe_yaml(value):
            logger.warning(
                "seed_frontmatter_value_not_serializable",
                source=source,
                key=key,
                received_type=type(value).__name__,
            )
            continue
        metadata[key] = value
    return True


__all__ = ["merge_seed_frontmatter"]
