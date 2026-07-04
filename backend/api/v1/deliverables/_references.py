"""Structured "참고한 지식" references for the deliverable report (R13).

The report reuses the verify contract's retrieved-knowledge statements as
"참고한 지식" chips. A canon-concept statement must deep-link to the concept
viewer — so it carries an EXPLICIT ``concept_id`` here, derived with the
resolver's OWN normalization (the single source of truth), instead of the
frontend re-slugifying the display text (which 404'd on body-laden statements).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from backend.knowledge.canonicalization.paths import is_valid_concept_id
from backend.knowledge.canonicalization.resolver import TagResolver


class ReferenceOut(BaseModel):
    """One "참고한 지식" statement. ``text`` is what the chip shows; ``concept_id``
    (set for a canon concept the viewer can open) deep-links by id — ``None`` for
    a prior decision / rejection, which stays plain text."""

    model_config = ConfigDict(extra="forbid")

    text: str
    concept_id: str | None = None


# The retrievers stamp these prefixes on NON-concept statements — a prior
# decision / rejection stays plain text (no concept link).
_NON_CONCEPT_PREFIXES = (
    re.compile(r"^prior decision\s*[—–-]", re.IGNORECASE),
    re.compile(r"^avoid \(prior rejection\)\s*[—–-]", re.IGNORECASE),
)
# canon_retriever folds the concept BODY in after a spaced em/en-dash:
# "{display} — {body}". The concept id is the slug of the LABEL only — split on
# the FIRST such dash, NOT the whole sentence (which slugifies to a bogus id).
_CONCEPT_BODY_SEP = re.compile(r"\s+[—–]\s+")


def to_reference(statement: str) -> ReferenceOut:
    """Structure a referenced-knowledge statement so the chip links by an explicit
    id: a canon concept carries ``concept_id = normalize(label)`` (the resolver's
    own normalization); a prior decision / rejection stays plain."""
    text = statement.strip()
    if any(prefix.match(text) for prefix in _NON_CONCEPT_PREFIXES):
        return ReferenceOut(text=text)
    label = _CONCEPT_BODY_SEP.split(text, maxsplit=1)[0].strip()
    concept_id = TagResolver.normalize(label)
    if concept_id and is_valid_concept_id(concept_id):
        return ReferenceOut(text=text, concept_id=concept_id)
    return ReferenceOut(text=text)
