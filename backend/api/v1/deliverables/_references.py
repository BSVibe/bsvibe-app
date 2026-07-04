"""Structured "참고한 지식" references for the deliverable report (R13).

The report reuses the verify contract's retrieved-knowledge statements as
"참고한 지식" chips. The retrievers stamp them as English strings — a canon
concept ("{display} — {body}"), a prior decision ("Prior decision — Q: … A: …"),
or a prior rejection ("Avoid (prior rejection) — …"). Structure each here so the
frontend can OPEN the stored knowledge:

* a **concept** deep-links to the concept viewer by an EXPLICIT ``concept_id``
  (the resolver's OWN normalization — never a frontend re-slugify of display
  text) and shows the LABEL only;
* a **decision / rejection** links to the garden NOTE it was absorbed into
  (``kind="note"`` + ``path``) — the founder's own stored knowledge, opened in
  the note viewer — with the question / reason as the chip label. The note path
  is resolved at the report boundary via :class:`DecisionNoteLocator` (the
  folded statement discards it); when the note can't be located (promoted,
  retracted, removed) the reference is dropped rather than shown as a dead tag.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from backend.knowledge.canonicalization.paths import is_valid_concept_id
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.workflow.application.verification_service import (
    LEGACY_RETRIEVED_KNOWLEDGE_RATIONALE,
    RETRIEVED_KNOWLEDGE_RATIONALE,
)

if TYPE_CHECKING:
    from ._schemas import VerificationReport


class ReferenceOut(BaseModel):
    """One "참고한 지식" statement, structured for the report chip.

    ``kind`` drives the chip: ``concept`` shows ``text`` (the LABEL only) and
    deep-links by ``concept_id``; ``note`` shows ``text`` (the decision question
    / rejection reason) and opens the garden note at ``path``; ``plain`` is a
    bare statement. ``concept_id`` / ``path`` are set only for their kind."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["concept", "note", "plain"] = "plain"
    text: str
    concept_id: str | None = None
    path: str | None = None


# The retrievers stamp these English prefixes on prior decisions / rejections;
# a decision folds "Q: … A: …" in. Detect them so each links to its stored note
# (and is never mis-slugified into a bogus concept chip).
_PRIOR_DECISION_PREFIX = re.compile(r"^prior decision\s*[—–-]\s*", re.IGNORECASE)
_PRIOR_REJECTION_PREFIX = re.compile(r"^avoid \(prior rejection\)\s*[—–-]\s*", re.IGNORECASE)
_DECISION_QA = re.compile(
    r"^Q:\s*(?P<question>.*?)\s+A:\s*(?P<answer>.*)$", re.IGNORECASE | re.DOTALL
)
# canon_retriever folds the concept BODY in after a spaced em/en-dash:
# "{display} — {body}". The concept id is the slug of the LABEL only — split on
# the FIRST such dash, NOT the whole sentence (which slugifies to a bogus id).
_CONCEPT_BODY_SEP = re.compile(r"\s+[—–]\s+")


def is_prior_note_reference(statement: str) -> bool:
    """True if the statement is a prior decision / rejection — the kinds that
    link to a stored garden note (so the report boundary knows to resolve paths
    only when at least one is present, skipping the vault scan otherwise)."""
    text = statement.strip()
    return bool(_PRIOR_DECISION_PREFIX.match(text) or _PRIOR_REJECTION_PREFIX.match(text))


def to_reference(statement: str, note_paths: dict[str, str] | None = None) -> ReferenceOut | None:
    """Structure a referenced-knowledge statement into a report chip, or drop it.

    ``note_paths`` maps a folded decision/rejection statement to its garden note
    path (from :class:`DecisionNoteLocator`). A prior decision / rejection links
    to that note (``kind="note"`` + the question / reason as ``text``); when the
    note can't be located it's dropped (``None``). A canon concept carries
    ``text = label`` + ``concept_id = normalize(label)`` (the body stays in the
    viewer); anything else is a bare ``plain`` statement."""
    text = statement.strip()
    lookup = note_paths or {}
    decision = _PRIOR_DECISION_PREFIX.match(text)
    if decision:
        path = lookup.get(text)
        if not path:
            return None
        body = text[decision.end() :].strip()
        qa = _DECISION_QA.match(body)
        label = qa.group("question").strip() if qa else body
        return ReferenceOut(kind="note", text=label, path=path)
    rejection = _PRIOR_REJECTION_PREFIX.match(text)
    if rejection:
        path = lookup.get(text)
        if not path:
            return None
        return ReferenceOut(kind="note", text=text[rejection.end() :].strip(), path=path)
    label = _CONCEPT_BODY_SEP.split(text, maxsplit=1)[0].strip()
    concept_id = TagResolver.normalize(label)
    if concept_id and is_valid_concept_id(concept_id):
        return ReferenceOut(kind="concept", text=label, concept_id=concept_id)
    return ReferenceOut(kind="plain", text=text)


def reference_from_entry(
    entry: dict[str, Any], note_paths: dict[str, str] | None = None
) -> ReferenceOut | None:
    """Build a report reference from one :func:`references_of` entry.

    STRUCTURED entries (new rows) carry identity — a concept links by ``ref``
    (concept_id), a note opens ``ref`` (path); no re-derivation. LEGACY entries
    (pre-refactor rows, ``kind`` absent) fall back to :func:`to_reference`, which
    re-slugifies a concept label and reverse-looks-up a note path via
    ``note_paths``. Returns ``None`` when a reference should be dropped."""
    kind = entry.get("kind")
    ref = entry.get("ref")
    text = str(entry.get("text") or "")
    label = str(entry.get("label") or text)
    if kind == "concept" and ref:
        return ReferenceOut(kind="concept", text=label, concept_id=str(ref))
    if kind == "note" and ref:
        return ReferenceOut(kind="note", text=label, path=str(ref))
    if kind == "plain":
        return ReferenceOut(kind="plain", text=label)
    # Legacy row (no structured kind, or a structured item missing its ref) —
    # derive identity the old way (concept slug / decision-note reverse lookup).
    return to_reference(text, note_paths)


def references_of(verifications: list[VerificationReport]) -> list[dict[str, Any]]:
    """The referenced-knowledge entries across a run's verifications (G2).

    Pulls from every judge check stamped with :data:`RETRIEVED_KNOWLEDGE_RATIONALE`
    (the retriever's canon / prior-decision / prior-rejection fold), deduped by
    statement text in first-seen order. Each entry is either STRUCTURED (new
    rows, from the check's ``knowledge_refs``: ``{text, kind, ref, label}`` —
    identity carried) or LEGACY (pre-refactor rows, only ``criteria`` strings:
    ``{text}`` — identity re-derived downstream). Defensive against malformed
    contract JSON: any non-conforming shape contributes nothing, never raises."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for verification in verifications:
        checks = verification.contract.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            # Current marker OR the legacy ("BSage") one on historical rows.
            if check.get("rationale") not in (
                RETRIEVED_KNOWLEDGE_RATIONALE,
                LEGACY_RETRIEVED_KNOWLEDGE_RATIONALE,
            ):
                continue
            refs = check.get("knowledge_refs")
            if isinstance(refs, list) and refs:
                for raw in refs:
                    if not isinstance(raw, dict):
                        continue
                    text = str(raw.get("text") or "").strip()
                    if not text or text in seen:
                        continue
                    seen.add(text)
                    entries.append(
                        {
                            "text": text,
                            "kind": raw.get("kind"),
                            "ref": raw.get("ref"),
                            "label": raw.get("label"),
                        }
                    )
                continue  # structured present → don't also read this check's criteria
            criteria = check.get("criteria")
            if not isinstance(criteria, list):
                continue
            for item in criteria:
                statement = str(item).strip()
                if statement and statement not in seen:
                    seen.add(statement)
                    entries.append({"text": statement})
    return entries
