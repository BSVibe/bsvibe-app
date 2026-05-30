"""ResolvedDecisionsRetriever — surface prior resolved decisions for new runs (B11b).

A prior audit found that resolved decisions live ONLY in the
``execution_decisions`` table + the current run's ``resolved_decisions``
resumption — so every new run re-asks the same question.

B11b closes the loop:

* On resolve, ``POST /api/v1/checkpoints/{id}/resolve`` writes a ``settle``
  :class:`~backend.execution.db.ExecutionRunActivity` with payload kind
  ``"decision_resolution"`` (the absorption pipeline already drains it into the
  workspace BSage vault as a garden note).
* This retriever reads that same vault state and surfaces RELEVANT resolved
  decisions for incoming change signals — so a future run's verify contract
  (B3 fold) and B6 knowledge seed include the prior answer instead of forcing
  the agent to re-ask.

Discipline (mirrors :class:`~backend.knowledge.retrieval.canon_retriever.CanonConceptRetriever`):

* **Graceful-empty** — no decisions / no matches / any failure → ``[]``.
* **Never raises into verify** — read/parse failures degrade to ``[]``; the
  verify path must not crash because knowledge was corrupt.
* **Workspace-scoped** — reads only its bound per-workspace
  :class:`~backend.knowledge.graph.storage.StorageBackend`, so it can never
  see another workspace's decisions.
* **Bounded** — at most :data:`_MAX_DECISIONS` resolved-decision statements
  per retrieve (a calm criterion list, not the whole log).
* **Signal-filtered** — a resolved decision surfaces only when its
  question / answer / intent tokens overlap the incoming signals; an unrelated
  signal pulls nothing in.
"""

from __future__ import annotations

import re

import structlog

from backend.knowledge.graph.markdown_utils import extract_frontmatter
from backend.knowledge.graph.storage import StorageBackend

logger = structlog.get_logger(__name__)

#: Cap on resolved-decision statements folded into one retrieve. Conservative
#: on purpose — the goal is to remind the agent of the prior answer, not to
#: dump the workspace's decision log into every contract.
_MAX_DECISIONS = 5

#: Frontmatter ``kind`` that marks a garden note as a decision-resolution
#: settlement (written by the SettleWorker for a ``decision_resolution`` settle
#: activity from the resolve endpoint).
_DECISION_KIND = "decision_resolution"

#: Garden subdir the settle sink writes seedling-maturity notes into. The
#: retriever scans only this subdir — decision-resolution notes start at
#: ``seedling`` and may be promoted later, but the worst case is a miss after
#: promotion (graceful-empty), never a crash.
_SEEDLING_DIR = "garden/seedling"

#: Minimum token length for signal-overlap matching — single-char and
#: 2-char tokens (``a``, ``to``, ``of``) are too generic to be useful filters.
_MIN_TOKEN_LEN = 3

#: Salient-token tokenizer (matches the canon retriever's grammar so the two
#: branches see the same "what does this signal talk about" view).
_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: English stopwords stripped from BOTH query- and decision-side token sets
#: before the overlap intersection. Without this, a 3-char function word like
#: ``the`` / ``and`` / ``for`` slips past :data:`_MIN_TOKEN_LEN` and matches
#: any decision whose body coincidentally contains it — a false positive that
#: surfaces irrelevant prior answers in new runs (D5 retriever hardening
#: carry-over). Korean stopwords are out of scope for v1 (founder primary
#: language but no retrieval-path tokenization of Korean yet); add when the
#: tokenizer learns CJK. Mirrors the deny-list discipline already used by the
#: settle-worker's summary tokenizer (``_SUMMARY_STOPWORDS``) — kept tight on
#: purpose: better to leave a borderline word in than over-prune signal.
_STOPWORDS: frozenset[str] = frozenset(
    {
        # articles / determiners
        "the",
        "this",
        "that",
        "these",
        "those",
        "any",
        "all",
        "some",
        # conjunctions
        "and",
        "but",
        "for",
        "nor",
        "yet",
        # prepositions (3+ chars only — shorter ones already filtered by length)
        "from",
        "into",
        "onto",
        "with",
        "about",
        "over",
        "under",
        # auxiliary / copula verbs
        "are",
        "was",
        "were",
        "been",
        "being",
        "has",
        "had",
        "have",
        "did",
        "does",
        "can",
        "could",
        "will",
        "would",
        "should",
        "may",
        "might",
        "must",
        # negation / generic
        "not",
        "new",
        "old",
        "out",
        "off",
        "now",
        "then",
        "than",
        # pronouns / possessives
        "its",
        "our",
        "your",
        "their",
        "you",
        "they",
        "them",
        "his",
        "her",
        "him",
        "she",
        "who",
        "what",
        "which",
        "where",
        "when",
        "why",
        "how",
    }
)


def _tokens(text: str) -> set[str]:
    """Lowercase salient tokens from ``text`` (length-filtered, deduped,
    stopword-stripped). Stopwords are removed from BOTH query and decision
    token sets so the intersection never matches on a generic function word."""
    return {
        t
        for t in _TOKEN_RE.findall(text.casefold())
        if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS
    }


class ResolvedDecisionsRetriever:
    """Read-only retrieval of relevant resolved decisions from a workspace vault.

    Holds only its bound per-workspace ``storage``; one ``retrieve_for_signals``
    call walks the garden seedling subdir, parses frontmatter, filters by
    ``kind = decision_resolution`` and signal overlap, and returns a capped
    list of human-legible statements. Satisfies the
    :class:`~backend.execution.verifier.service.CanonRetriever` Protocol
    structurally (same method shape).
    """

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        """Return ≤ :data:`_MAX_DECISIONS` resolved-decision statements for the
        signals. Graceful-empty + never raises (verify path discipline)."""
        try:
            return await self._retrieve(signals)
        except Exception:  # noqa: BLE001 — verify path must never crash on read
            logger.warning("resolved_decisions_retrieve_failed", exc_info=True)
            return []

    async def _retrieve(self, signals: str) -> list[str]:
        signal_tokens = _tokens(signals)
        if not signal_tokens:
            return []
        paths = await self._storage.list_files(_SEEDLING_DIR)
        if not paths:
            return []
        statements: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if len(statements) >= _MAX_DECISIONS:
                break
            try:
                content = await self._storage.read(path)
            except FileNotFoundError:
                continue
            except Exception:  # noqa: BLE001 — malformed / unreadable note is soft-skip
                logger.warning("resolved_decision_read_failed", path=path, exc_info=True)
                continue
            fm = extract_frontmatter(content)
            if fm.get("kind") != _DECISION_KIND:
                continue
            question = str(fm.get("question") or "").strip()
            answer = str(fm.get("answer") or "").strip()
            if not question or not answer:
                continue
            # Signal-overlap filter: at least one signal token must appear in
            # the decision's question / answer / intent (the founder-stable
            # signals, never the LLM-generated body). Without this, every
            # decision would surface on every signal — a token dump.
            intent = str(fm.get("intent_text") or "").strip()
            decision_tokens = _tokens(f"{question}\n{answer}\n{intent}")
            if not (signal_tokens & decision_tokens):
                continue
            statement = f"Prior decision — Q: {question} A: {answer}"
            if statement in seen:
                continue
            seen.add(statement)
            statements.append(statement)
        return statements


__all__ = ["ResolvedDecisionsRetriever"]
