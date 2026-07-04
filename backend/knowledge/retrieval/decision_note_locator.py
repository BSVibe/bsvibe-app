"""Locate the garden note behind a folded prior-decision / rejection statement.

The :class:`~backend.knowledge.retrieval.resolved_decisions_retriever.ResolvedDecisionsRetriever`
and :class:`~backend.knowledge.retrieval.negative_pattern_retriever.NegativePatternRetriever`
fold prior decisions / rejections into the verify contract as FLAT statements
("Prior decision — Q: … A: …" / "Avoid (prior rejection) — …"), discarding the
source note path. The delivery report wants to link each such reference back to
the STORED knowledge note (the settle-worker garden note it came from) so the
founder can open it — instead of rendering a dead English tag.

This locator rebuilds the SAME statement string from each garden note's
frontmatter (mirroring the two retrievers EXACTLY) and returns a
``{statement -> vault path}`` map. The match is deterministic — the folded
statement came from the same construction — so an exact key lookup resolves the
path. A note that was promoted out of ``garden/seedling``, retracted, or removed
simply has no entry (graceful: the report drops that reference rather than
linking to nothing).
"""

from __future__ import annotations

import structlog

from backend.knowledge.graph.markdown_utils import extract_frontmatter
from backend.knowledge.graph.storage import StorageBackend

logger = structlog.get_logger(__name__)

#: Garden subdir the settle sink writes decision/rejection notes into. Mirrors
#: both retrievers' ``_SEEDLING_DIR`` — the notes start (and usually stay) here.
_SEEDLING_DIR = "garden/seedling"
_DECISION_KIND = "decision_resolution"
_NEGATIVE_KIND = "negative_pattern"


def _statement_for(fm: dict[str, object]) -> str | None:
    """Rebuild the folded statement for a note's frontmatter, or None if the note
    is neither a decision resolution nor a rejection. MUST stay byte-identical to
    the retrievers' ``statement = f"…"`` so an exact-match lookup resolves."""
    kind = fm.get("kind")
    if kind == _DECISION_KIND:
        question = str(fm.get("question") or "").strip()
        answer = str(fm.get("answer") or "").strip()
        if question and answer:
            return f"Prior decision — Q: {question} A: {answer}"
    elif kind == _NEGATIVE_KIND:
        reason = str(fm.get("reason") or "").strip()
        if reason:
            return f"Avoid (prior rejection) — {reason}"
    return None


class DecisionNoteLocator:
    """Maps folded decision/rejection statements to their garden note paths.

    Workspace-scoped (reads only the bound per-workspace ``StorageBackend``) and
    graceful — any read/parse failure degrades to an empty map, never raising
    into the report render."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    async def statement_paths(self) -> dict[str, str]:
        try:
            return await self._scan()
        except Exception:  # noqa: BLE001 — locating is best-effort; never break the report
            logger.warning("decision_note_locate_failed", exc_info=True)
            return {}

    async def _scan(self) -> dict[str, str]:
        paths = await self._storage.list_files(_SEEDLING_DIR)
        out: dict[str, str] = {}
        for path in paths:
            try:
                content = await self._storage.read(path)
            except FileNotFoundError:
                continue
            except Exception:  # noqa: BLE001 — a malformed / unreadable note is soft-skip
                logger.warning("decision_note_read_failed", path=path, exc_info=True)
                continue
            fm = extract_frontmatter(content)
            # D5 ratchet: a tombstoned note is invisible to future runs.
            if fm.get("retracted_at"):
                continue
            statement = _statement_for(fm)
            if statement and statement not in out:
                out[statement] = path
        return out


__all__ = ["DecisionNoteLocator"]
