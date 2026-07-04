"""``RetrievedKnowledge`` — a retrieved statement that CARRIES its identity.

The retrievers know each statement's stable identity at retrieval time (a
concept's ``concept_id``, a decision/rejection note's vault ``path``) but the
legacy seam (``retrieve_for_signals -> list[str]``) folded only the flat display
string into the verify contract, discarding it. The delivery report then had to
re-derive identity — re-slugifying the concept label, and SCANNING the vault to
map a folded "Prior decision — …" string back to its note (a structural smell:
one string doubling as display AND identity).

``retrieve_structured`` carries identity forward instead: the retriever emits
``RetrievedKnowledge`` items, the verify contract persists them (``knowledge_refs``
on the judge check), and the report reads the ref directly — no scan, no
re-slug. ``retrieve_for_signals`` stays a thin ``[i.text for i in …]`` projection
for the ~6 LLM-context consumers that only need the text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: How the delivery report renders the reference. ``concept`` deep-links to the
#: concept viewer by ``ref`` (a ``concept_id``); ``note`` opens the garden note
#: at ``ref`` (a vault path); ``plain`` is a bare, unlinked statement.
RefKind = Literal["concept", "note", "plain"]


@dataclass(frozen=True)
class RetrievedKnowledge:
    """One retrieved statement with its identity carried forward.

    ``text`` — the full statement folded into LLM context / the verify contract's
    judge ``criteria`` (unchanged wire: ``"{display} — {body}"`` /
    ``"Prior decision — Q: … A: …"`` / ``"Avoid (prior rejection) — …"``).
    ``kind`` — how the report renders it. ``ref`` — the stable identity
    (``concept_id`` for a concept, garden note ``path`` for a note; ``None`` for
    plain). ``label`` — the report chip's display text (the concept display, the
    decision question, the rejection reason); falls back to ``text`` when unset.
    """

    text: str
    kind: RefKind = "plain"
    ref: str | None = None
    label: str | None = None

    def to_wire(self) -> dict[str, str | None]:
        """Serialize for persistence on the verify contract's judge check."""
        return {"text": self.text, "kind": self.kind, "ref": self.ref, "label": self.label}


__all__ = ["RefKind", "RetrievedKnowledge"]
