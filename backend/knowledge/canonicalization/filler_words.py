"""Shared filler/meta/action word filter for concept-candidate quality.

The trust-ratchet settle pipeline derives content tags by tokenizing free-text
work summaries (:func:`backend.workers.settle_worker.derive_content_tags`), and
the :class:`~backend.knowledge.canonicalization.promotion.GardenObservationPromoter`
then turns each distinct tag into a canonical concept. Tokenization is cheap and
deterministic but indiscriminate — it keeps every non-stopword token, so generic
filler ("else", "nothing", "exactly"), action verbs reporting what an agent
*did* ("created", "summarize", "reply"), and meta words about the run itself
("verified", "untitled", "inspection") all leak through and settle as noisy
single-word "concepts" on the "What I know" surface.

This module is the single source of truth for those **non-concept** words: a
deterministic (no LLM, no network) deny-list of filler / meta / action terms
that name *the act of working* rather than *the subject the work is about*. It
is applied at two points so it cleans both already-written observations and
future ones:

* the promoter chokepoint
  (:meth:`GardenObservationPromoter._collect_candidate_tags`) — the essential
  one, because existing prod observations already carry the noisy tags, and
* the sink (:func:`_tags_from_summary`) so future observations don't even
  record pure filler.

Design discipline (intentionally conservative — better to keep a borderline
subject noun than to prune signal):

* It MUST contain only words that name the *process* of work, never a subject.
  Real concepts like ``python``, ``hello``, ``world``, ``function``,
  ``calculator``, ``application``, ``executor``, ``workspace``, ``project``,
  ``readme``, ``requirements``, product slugs, and entity names must survive.
* It is NOT a recurrence threshold. Dropping filler is purely lexical; we do
  not prune rare-but-meaningful subjects.

Words are stored already-normalized (lowercase, hyphen-collapsed) so a caller
can test membership directly against a normalized tag.
"""

from __future__ import annotations

# Non-concept words: generic English filler, action/meta verbs reporting what
# was done, and run-meta nouns. Kept as one flat normalized frozenset so the
# promoter and the sink share an identical, non-divergent deny-list. Grouped by
# comment only for readability; the spirit is "names the act of working / a
# filler particle, never the subject".
_FILLER_WORDS: frozenset[str] = frozenset(
    {
        # --- generic filler particles / quantifiers / connectives ---------
        "else",
        "nothing",
        "something",
        "anything",
        "everything",
        "exactly",
        "one",
        "two",
        "three",
        "first",
        "second",
        "third",
        "next",
        "last",
        "based",
        "about",
        "above",
        "below",
        "after",
        "before",
        "again",
        "also",
        "just",
        "only",
        "more",
        "most",
        "much",
        "some",
        "such",
        "very",
        "here",
        "there",
        "where",
        "when",
        "what",
        "which",
        "while",
        "because",
        "however",
        "therefore",
        "thus",
        "yet",
        "still",
        # --- action / meta verbs (the *act* of working, not a subject) ----
        "created",
        "create",
        "creating",
        "complete",
        "completed",
        "completing",
        "finish",
        "finished",
        "finishing",
        "reply",
        "replied",
        "respond",
        "responded",
        "summarize",
        "summarized",
        "summary",
        "verified",
        "verify",
        "verifying",
        "inspect",
        "inspection",
        "inspected",
        "review",
        "reviewed",
        "check",
        "checked",
        "checking",
        "update",
        "updated",
        "updating",
        "change",
        "changed",
        "changing",
        "implement",
        "implemented",
        "ensure",
        "ensured",
        "provide",
        "provided",
        "perform",
        "performed",
        "start",
        "started",
        "stop",
        "stopped",
        "continue",
        "continued",
        "consider",
        "considered",
        "process",
        "processed",
        "handle",
        "handled",
        # --- run-meta / placeholder nouns ---------------------------------
        "untitled",
        "line",
        "lines",
        "note",
        "notes",
        "item",
        "items",
        "thing",
        "things",
        "stuff",
        "task",
        "tasks",
        "result",
        "results",
        "output",
        "input",
        "value",
        "values",
        "content",
        "detail",
        "details",
        "example",
        "examples",
        "version",
        "versions",
        "part",
        "parts",
        "way",
        "ways",
        "time",
        "times",
        "place",
    }
)

# A normalized candidate shorter than this is too generic to anchor a concept.
# (Real one/two-character subject tokens are vanishingly rare; the §2 grammar
# already drops empties.) Kept low so short real concepts are not pruned.
MIN_CONCEPT_TAG_LEN = 3


def is_filler_tag(normalized_tag: str) -> bool:
    """Return ``True`` if ``normalized_tag`` is a non-concept filler/meta word.

    Expects an already-normalized tag (lowercase, hyphen-collapsed, no edge
    hyphens — i.e. the output of ``TagResolver.normalize``). A tag is filler
    when it is in :data:`_FILLER_WORDS` or shorter than
    :data:`MIN_CONCEPT_TAG_LEN`.
    """
    if len(normalized_tag) < MIN_CONCEPT_TAG_LEN:
        return True
    return normalized_tag in _FILLER_WORDS


__all__ = ["MIN_CONCEPT_TAG_LEN", "is_filler_tag"]
