"""NegativePatternRetriever — surface prior rejection feedback for new runs (G1).

When the founder discards a deliverable with a *reason*, the resolve endpoint
emits a ``negative_pattern`` settle activity; the absorption pipeline drains it
into the workspace BSage vault as a garden note (frontmatter
``kind: negative_pattern`` + ``reason``). This retriever reads that same vault
state and surfaces RELEVANT rejections for incoming change signals — so a future
run's verify contract (B3 fold) and B6 knowledge seed carry the founder's "don't
do this again" guidance instead of repeating the rejected approach.

It mirrors :class:`~backend.knowledge.retrieval.resolved_decisions_retriever.ResolvedDecisionsRetriever`
discipline: graceful-empty, never raises into verify, workspace-scoped, bounded,
and signal-filtered (a rejection surfaces only when its reason/question/intent
tokens overlap the incoming signals).
"""

from __future__ import annotations

import re

import structlog

from backend.knowledge.graph.markdown_utils import extract_frontmatter
from backend.knowledge.graph.storage import StorageBackend

logger = structlog.get_logger(__name__)

#: Cap on negative-pattern statements folded into one retrieve. Conservative on
#: purpose — remind the agent of the relevant rejection(s), not dump the log.
_MAX_PATTERNS = 5

#: Frontmatter ``kind`` marking a garden note as a discard-with-reason rejection.
_NEGATIVE_KIND = "negative_pattern"

#: Garden subdir the settle sink writes seedling-maturity notes into. Worst case
#: after a later promotion is a miss (graceful-empty), never a crash.
_SEEDLING_DIR = "garden/seedling"

#: Minimum token length for signal-overlap matching (drop ``a``/``to``/``of``).
_MIN_TOKEN_LEN = 3

#: Salient-token tokenizer (matches the canon / resolved-decisions grammar so
#: every retriever sees the same "what does this signal talk about" view).
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    """Lowercase salient tokens from ``text`` (length-filtered, deduped)."""
    return {t for t in _TOKEN_RE.findall(text.casefold()) if len(t) >= _MIN_TOKEN_LEN}


class NegativePatternRetriever:
    """Read-only retrieval of relevant rejection feedback from a workspace vault.

    Satisfies the :class:`~backend.workflow.application.verification_service.CanonRetriever`
    Protocol structurally (same ``retrieve_for_signals`` shape), so it composes
    into :class:`~backend.knowledge.retrieval.composite_retriever.CompositeCanonRetriever`
    alongside the canon-concept and resolved-decisions sources.
    """

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        """Return ≤ :data:`_MAX_PATTERNS` rejection statements for the signals.
        Graceful-empty + never raises (verify path discipline)."""
        try:
            return await self._retrieve(signals)
        except Exception:  # noqa: BLE001 — verify path must never crash on read
            logger.warning("negative_pattern_retrieve_failed", exc_info=True)
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
            if len(statements) >= _MAX_PATTERNS:
                break
            try:
                content = await self._storage.read(path)
            except FileNotFoundError:
                continue
            except Exception:  # noqa: BLE001 — malformed / unreadable note is soft-skip
                logger.warning("negative_pattern_read_failed", path=path, exc_info=True)
                continue
            fm = extract_frontmatter(content)
            if fm.get("kind") != _NEGATIVE_KIND:
                continue
            reason = str(fm.get("reason") or "").strip()
            if not reason:
                continue
            # Signal-overlap filter on the founder-stable text (reason / question
            # / intent — never an LLM-generated body). Without it every rejection
            # would surface on every signal — a token dump into every contract.
            question = str(fm.get("question") or "").strip()
            intent = str(fm.get("intent_text") or "").strip()
            pattern_tokens = _tokens(f"{reason}\n{question}\n{intent}")
            if not (signal_tokens & pattern_tokens):
                continue
            statement = f"Avoid (prior rejection) — {reason}"
            if statement in seen:
                continue
            seen.add(statement)
            statements.append(statement)
        return statements


__all__ = ["NegativePatternRetriever"]
