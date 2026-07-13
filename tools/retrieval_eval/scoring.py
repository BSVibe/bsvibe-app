"""Scoring for the retrieval eval harness — see tests/tools/test_retrieval_eval_scoring.py."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class QuestionResult:
    """One eval question, what it expected, and what the retriever actually returned."""

    question: str
    #: ``specific`` — the answer is in a note (scored). ``broad`` — the answer is an
    #: aggregate over the workspace; retrieval cannot answer it, so it is reported
    #: but never scored.
    kind: str
    #: Distinctive slugs of the notes a competent retriever must surface.
    expected: list[str]
    #: Vault-relative paths the retriever returned, in rank order.
    retrieved: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Summary:
    specific_count: int
    broad_count: int
    hit_rate: float | None
    mean_recall: float | None
    misses: list[str]


def _found(retrieved: list[str], slug: str) -> bool:
    """Labels are distinctive SLUGS, not full paths: the vault path carries a
    region/workspace prefix and notes get renamed as the corpus settles."""
    return any(slug in ref for ref in retrieved)


def recall_at_k(*, retrieved: list[str], expected: list[str]) -> float | None:
    if not expected:
        return None
    hits = sum(1 for slug in expected if _found(retrieved, slug))
    return hits / len(expected)


def hit_at_k(*, retrieved: list[str], expected: list[str]) -> int | None:
    if not expected:
        return None
    return 1 if any(_found(retrieved, slug) for slug in expected) else 0


def summarize(results: list[QuestionResult]) -> Summary:
    specific = [r for r in results if r.expected]
    broad = [r for r in results if not r.expected]
    if not specific:
        return Summary(0, len(broad), None, None, [])
    hits = [hit_at_k(retrieved=r.retrieved, expected=r.expected) or 0 for r in specific]
    recalls = [recall_at_k(retrieved=r.retrieved, expected=r.expected) or 0.0 for r in specific]
    misses = [r.question for r, h in zip(specific, hits, strict=True) if h == 0]
    return Summary(
        specific_count=len(specific),
        broad_count=len(broad),
        hit_rate=sum(hits) / len(specific),
        mean_recall=sum(recalls) / len(specific),
        misses=misses,
    )


__all__ = ["QuestionResult", "Summary", "hit_at_k", "recall_at_k", "summarize"]
