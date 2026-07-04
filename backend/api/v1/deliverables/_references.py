"""Structured "참고한 지식" references for the deliverable report (R13).

The report reuses the verify contract's retrieved-knowledge statements as
"참고한 지식" chips. The retrievers stamp them as English strings — a canon
concept ("{display} — {body}"), a prior decision ("Prior decision — Q: … A: …"),
or a prior rejection ("Avoid (prior rejection) — …"). Structure each here so the
frontend renders it in the reader's locale (next-intl) and a concept deep-links
by an EXPLICIT ``concept_id`` (the resolver's OWN normalization — never a
frontend re-slugify of display text). The founder-facing free text (the concept
label, the decision question, the rejection reason) stays as written; only the
system framing (the prefix + the decision's resolution ``answer``) is localized.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.knowledge.canonicalization.paths import is_valid_concept_id
from backend.knowledge.canonicalization.resolver import TagResolver


class ReferenceOut(BaseModel):
    """One "참고한 지식" statement, structured for locale-aware rendering.

    ``kind`` drives the chip: ``concept`` shows ``text`` (the LABEL only, a short
    pill) and deep-links by ``concept_id``; ``decision`` shows a localized prefix
    + ``text`` (the question) + the localized ``answer`` (the resolution);
    ``rejection`` shows a localized prefix + ``text`` (the reason); ``plain`` is a
    bare statement. ``concept_id`` / ``answer`` are set only for their kind."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["concept", "decision", "rejection", "plain"] = "plain"
    text: str
    concept_id: str | None = None
    answer: str | None = None


# The retrievers stamp these English prefixes; strip them so the frontend can
# render a localized prefix instead. A decision folds "Q: … A: …" in.
_PRIOR_DECISION_PREFIX = re.compile(r"^prior decision\s*[—–-]\s*", re.IGNORECASE)
_PRIOR_REJECTION_PREFIX = re.compile(r"^avoid \(prior rejection\)\s*[—–-]\s*", re.IGNORECASE)
_DECISION_QA = re.compile(
    r"^Q:\s*(?P<question>.*?)\s+A:\s*(?P<answer>.*)$", re.IGNORECASE | re.DOTALL
)
# canon_retriever folds the concept BODY in after a spaced em/en-dash:
# "{display} — {body}". The concept id is the slug of the LABEL only — split on
# the FIRST such dash, NOT the whole sentence (which slugifies to a bogus id).
_CONCEPT_BODY_SEP = re.compile(r"\s+[—–]\s+")


def to_reference(statement: str) -> ReferenceOut:
    """Structure a referenced-knowledge statement for locale-aware rendering.

    A prior decision splits into its question (``text``) + resolution (``answer``);
    a prior rejection keeps its reason (``text``); a canon concept carries
    ``text = label`` + ``concept_id = normalize(label)`` (the body stays in the
    viewer); anything else is a bare ``plain`` statement."""
    text = statement.strip()
    decision = _PRIOR_DECISION_PREFIX.match(text)
    if decision:
        body = text[decision.end() :].strip()
        qa = _DECISION_QA.match(body)
        if qa:
            return ReferenceOut(
                kind="decision",
                text=qa.group("question").strip(),
                answer=qa.group("answer").strip(),
            )
        return ReferenceOut(kind="decision", text=body)
    rejection = _PRIOR_REJECTION_PREFIX.match(text)
    if rejection:
        return ReferenceOut(kind="rejection", text=text[rejection.end() :].strip())
    label = _CONCEPT_BODY_SEP.split(text, maxsplit=1)[0].strip()
    concept_id = TagResolver.normalize(label)
    if concept_id and is_valid_concept_id(concept_id):
        return ReferenceOut(kind="concept", text=label, concept_id=concept_id)
    return ReferenceOut(kind="plain", text=text)
