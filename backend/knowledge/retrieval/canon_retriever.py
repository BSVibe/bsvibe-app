"""CanonRetriever — high-precision canonical-pattern retrieval for verify (B3).

The verifier folds BSage canon into a verify contract through ONE read-only
seam (:class:`~backend.workflow.application.verification_service.CanonRetriever`):
``retrieve_for_signals(signals) -> list[str]``. Before B3 the production
:func:`backend.workflow.infrastructure.workers.run._factory` always passed ``retriever=None``, so the
workspace's settled knowledge was NEVER consulted at verify time — the product
premise (knowledge informs the agent) was dead at runtime (RC-2).

This module supplies the real retriever. Given the change *signals* (the work
summary + changed paths), it surfaces the workspace's **promoted active
concepts** that are relevant to the change — never arbitrary garden notes. A tag
only resolves to an active concept if it already cleared the promoter's
recurrence gate (Handoff §11 / :class:`TagResolver`), so precision is structural:
the retriever can only ever surface concepts the trust ratchet already settled.

Discipline (matches the settle-extractor's graceful resolution in
``backend.workflow.infrastructure.workers.run``):

* **Graceful-empty** — an empty vault / no active concepts / no matching signal
  → ``[]``. An empty-knowledge workspace therefore sees NO change to verify
  behaviour (the contract is identical to the no-retriever case).
* **Never raises into verify** — any read/index failure degrades to ``[]``; the
  verify path must not crash because knowledge was unavailable.
* **Workspace-scoped** — it reads only its bound per-workspace
  :class:`~backend.knowledge.graph.storage.StorageBackend`, so it can never see
  another workspace's canon.
* **Bounded** — at most :data:`_MAX_PATTERNS` statements folded in (a calm
  judge criterion list, not the whole registry).
"""

from __future__ import annotations

import re

import structlog

from backend.knowledge.canonicalization import paths
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.graph.markdown_utils import body_after_frontmatter
from backend.knowledge.graph.storage import StorageBackend
from backend.knowledge.retrieval.knowledge_item import RetrievedKnowledge

logger = structlog.get_logger(__name__)

# At most this many canonical statements fold into one verify contract.
_MAX_PATTERNS = 5

# Cap on the synthesized-body substance folded onto one concept statement, so a
# concept with many members stays a calm, bounded contribution to the contract.
_MAX_BODY_CHARS = 400

# Signals → candidate tokens. The work summary + changed paths are free text; we
# extract lowercase alnum runs (path separators / punctuation become breaks) and
# also build adjacent-pair bigrams so multi-word concept ids (``dependency
# pinning`` → ``dependency-pinning``) can resolve. Single-character tokens are
# dropped as noise.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _candidate_tags(signals: str) -> list[str]:
    """Distinct candidate tag strings from the change signals.

    Yields single tokens plus adjacent bigrams (joined with a hyphen), so both
    one-word and two-word canonical ids/aliases get a chance to resolve. Order
    is preserved (first sighting wins) and duplicates are dropped.
    """
    words = [w for w in _TOKEN_RE.findall(signals.casefold()) if len(w) > 1]
    candidates: list[str] = []
    seen: set[str] = set()
    for i, word in enumerate(words):
        for token in (word, f"{words[i - 1]}-{word}" if i > 0 else None):
            if token and token not in seen:
                seen.add(token)
                candidates.append(token)
    return candidates


class CanonConceptRetriever:
    """Resolve change signals to the workspace's relevant active concepts.

    Holds only its bound per-workspace ``storage``; it builds a fresh derived
    index per call (cheap for self-host v1 vault sizes, and keeps the retriever
    stateless so a long-lived instance never serves a stale snapshot). Satisfies
    the :class:`~backend.workflow.application.verification_service.CanonRetriever` Protocol
    structurally.
    """

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        """Return ≤ :data:`_MAX_PATTERNS` canonical statements for the change.

        Graceful: no canon / no match / any failure → ``[]``. Never raises."""
        return [item.text for item in await self.retrieve_structured(signals)]

    async def retrieve_structured(self, signals: str) -> list[RetrievedKnowledge]:
        """Like :meth:`retrieve_for_signals` but carries each concept's identity
        (``concept_id``) so the verify contract / report can deep-link without
        re-slugifying the display label. Graceful-empty; never raises."""
        try:
            return await self._retrieve(signals)
        except Exception:  # noqa: BLE001 — verify path must never crash on canon
            logger.warning("canon_retrieve_failed", exc_info=True)
            return []

    async def _retrieve(self, signals: str) -> list[RetrievedKnowledge]:
        candidates = _candidate_tags(signals)
        if not candidates:
            return []

        index = InMemoryCanonicalizationIndex()
        await index.initialize(self._storage)
        # Empty-knowledge workspace → no active concepts → no fold. Cheap exit
        # that also avoids resolving every token against an empty registry.
        if not await index.list_active_concepts():
            return []

        resolver = TagResolver(index=index)
        items: list[RetrievedKnowledge] = []
        seen_ids: set[str] = set()
        for tag in candidates:
            if len(items) >= _MAX_PATTERNS:
                break
            resolved = await resolver.resolve(tag)
            if resolved.status != "resolved" or resolved.concept_id is None:
                continue
            if resolved.concept_id in seen_ids:
                continue
            concept = await index.get_active_concept(resolved.concept_id)
            if concept is None:
                continue
            seen_ids.add(resolved.concept_id)
            # The concept's display H1 is its label (Handoff §0.2). KG Lift 4 —
            # fold in the synthesized hub BODY (member excerpts) too, so the
            # concept's actual substance, not just its title, reaches the
            # verify/answer context. Bodyless concepts surface the title alone.
            label = (concept.display or concept.concept_id).strip()
            if not label:
                continue
            body = await self._concept_body_text(resolved.concept_id)
            statement = f"{label} — {body}" if body else label
            # Carry the concept_id (the label IS the report chip; the body stays
            # folded in `text` for verify/answer context but out of the chip).
            items.append(
                RetrievedKnowledge(
                    text=statement, kind="concept", ref=resolved.concept_id, label=label
                )
            )
        return items

    async def _concept_body_text(self, concept_id: str) -> str:
        """The synthesized member excerpts from a concept's hub body (Lift 1),
        joined + bounded. ``""`` for a bodyless concept or any read failure
        (the verify path must never crash on canon)."""
        path = paths.active_concept_path(concept_id)
        try:
            if not await self._storage.exists(path):
                return ""
            text = await self._storage.read(path)
        except Exception:  # noqa: BLE001 — never break retrieval on a read
            return ""
        excerpts: list[str] = []
        for line in body_after_frontmatter(text).splitlines():
            stripped = line.strip()
            # MOC member line: ``- [[stem]] — excerpt``.
            if stripped.startswith("- ") and "—" in stripped:
                excerpt = stripped.split("—", 1)[1].strip()
                if excerpt:
                    excerpts.append(excerpt)
        return " ".join(excerpts)[:_MAX_BODY_CHARS]


__all__ = ["CanonConceptRetriever"]
