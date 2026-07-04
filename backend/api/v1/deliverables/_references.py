"""Structured "참고한 지식" references for the deliverable report (R13).

The report reuses the verify contract's retrieved-knowledge statements as
"참고한 지식" chips. The retrievers stamp them as English strings — a canon
concept ("{display} — {body}"), a prior decision ("Prior decision — Q: … A: …"),
or a prior rejection ("Avoid (prior rejection) — …"). Only the CONCEPTS are
founder-facing knowledge: :func:`to_reference` structures a concept so the
frontend deep-links by an EXPLICIT ``concept_id`` (the resolver's OWN
normalization — never a frontend re-slugify of display text) and shows the LABEL
only. Prior decisions/rejections are verify-context artifacts (a resolved
checkpoint, a rejected pattern), NOT user-facing knowledge, so they are DROPPED
(``to_reference`` returns ``None``) — the detection stays so a "Prior decision —"
string is never mis-slugified into a bogus concept chip.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.knowledge.canonicalization.paths import is_valid_concept_id
from backend.knowledge.canonicalization.resolver import TagResolver


class ReferenceOut(BaseModel):
    """One "참고한 지식" statement, structured for the report chip.

    ``kind`` drives the chip: ``concept`` shows ``text`` (the LABEL only, a short
    pill) and deep-links by ``concept_id``; ``plain`` is a bare statement.
    ``concept_id`` is set only for a concept."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["concept", "plain"] = "plain"
    text: str
    concept_id: str | None = None


# The retrievers stamp these English prefixes on prior decisions / rejections.
# They are verify-context artifacts, not founder-facing knowledge — detect them
# so they can be DROPPED (and never mis-slugified into a bogus concept chip).
_PRIOR_DECISION_PREFIX = re.compile(r"^prior decision\s*[—–-]\s*", re.IGNORECASE)
_PRIOR_REJECTION_PREFIX = re.compile(r"^avoid \(prior rejection\)\s*[—–-]\s*", re.IGNORECASE)
# canon_retriever folds the concept BODY in after a spaced em/en-dash:
# "{display} — {body}". The concept id is the slug of the LABEL only — split on
# the FIRST such dash, NOT the whole sentence (which slugifies to a bogus id).
_CONCEPT_BODY_SEP = re.compile(r"\s+[—–]\s+")


def to_reference(statement: str) -> ReferenceOut | None:
    """Structure a referenced-knowledge statement into a report chip, or drop it.

    Returns ``None`` for a prior decision / prior rejection — those are
    verify-context artifacts, not user-facing knowledge. A canon concept carries
    ``text = label`` + ``concept_id = normalize(label)`` (the body stays in the
    viewer); anything else is a bare ``plain`` statement."""
    text = statement.strip()
    if _PRIOR_DECISION_PREFIX.match(text) or _PRIOR_REJECTION_PREFIX.match(text):
        return None
    label = _CONCEPT_BODY_SEP.split(text, maxsplit=1)[0].strip()
    concept_id = TagResolver.normalize(label)
    if concept_id and is_valid_concept_id(concept_id):
        return ReferenceOut(kind="concept", text=label, concept_id=concept_id)
    return ReferenceOut(kind="plain", text=text)
