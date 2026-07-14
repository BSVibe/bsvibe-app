"""Scoring for the retrieval eval harness (L0).

Before tuning retrieval we need to be able to MEASURE it — otherwise "it feels
better" is indistinguishable from noise, and every knob (top_k, min_similarity,
hybrid search) is a coin flip. This module is the scorer; the eval set lives in
``tools/retrieval_eval/eval_set.yaml`` and the runner drives the real retriever
against a real workspace.

Two question classes, deliberately:

* ``specific`` — a question whose answer IS in a note. Labelled with the note(s) a
  competent retriever must surface. Scored with hit@k / recall@k.
* ``broad`` — "현 프로젝트 상황 설명해줘". No note answers it; the answer is an
  AGGREGATE over the workspace. These are NOT scored as retrieval failures — they
  are the evidence that a digest, not a better search, is what they need. Scoring
  them as retrieval would push us to tune the corpus into a shape that flatters a
  question retrieval cannot answer.
"""

from __future__ import annotations

import pytest

from tools.retrieval_eval.scoring import QuestionResult, hit_at_k, recall_at_k, summarize


def test_recall_counts_only_expected_notes_found() -> None:
    assert recall_at_k(retrieved=["a.md", "b.md", "x.md"], expected=["a.md", "b.md"]) == 1.0
    assert recall_at_k(retrieved=["a.md", "x.md"], expected=["a.md", "b.md"]) == 0.5
    assert recall_at_k(retrieved=["x.md"], expected=["a.md"]) == 0.0


def test_expected_notes_match_on_path_substring() -> None:
    """The eval set labels notes by a distinctive slug, not the full vault path —
    paths carry a region/workspace prefix and the corpus renames notes over time."""
    retrieved = ["garden/seedling/eventbus-publish는-구독자-예외를-삼켜야-한다.md"]
    assert recall_at_k(retrieved=retrieved, expected=["eventbus-publish"]) == 1.0


def test_hit_at_k_is_one_when_any_expected_note_surfaces() -> None:
    assert hit_at_k(retrieved=["x.md", "a.md"], expected=["a.md", "b.md"]) == 1
    assert hit_at_k(retrieved=["x.md"], expected=["a.md", "b.md"]) == 0


def test_recall_with_no_expected_notes_is_not_a_failure() -> None:
    """A ``broad`` question has no labelled notes — it must not drag the score down
    (it is measuring the wrong thing on purpose)."""
    assert recall_at_k(retrieved=["x.md"], expected=[]) is None
    assert hit_at_k(retrieved=["x.md"], expected=[]) is None


def test_summarize_scores_specific_questions_only() -> None:
    results = [
        QuestionResult(question="q1", kind="specific", expected=["a"], retrieved=["a.md"]),
        QuestionResult(question="q2", kind="specific", expected=["b"], retrieved=["x.md"]),
        QuestionResult(question="q3", kind="broad", expected=[], retrieved=["noise.md"]),
    ]

    summary = summarize(results)

    assert summary.specific_count == 2
    assert summary.hit_rate == 0.5  # q1 hit, q2 missed
    assert summary.mean_recall == 0.5
    assert summary.misses == ["q2"]
    # broad questions are reported, never scored
    assert summary.broad_count == 1


def test_summarize_with_no_specific_questions_does_not_divide_by_zero() -> None:
    summary = summarize([QuestionResult(question="q", kind="broad", expected=[], retrieved=[])])
    assert summary.hit_rate is None
    assert summary.mean_recall is None


@pytest.mark.parametrize("kind", ["specific", "broad"])
def test_question_result_records_what_came_back(kind: str) -> None:
    """Every run keeps the raw retrieved refs — a score with no evidence behind it is
    how you end up tuning to a number instead of to the founder's question."""
    result = QuestionResult(question="q", kind=kind, expected=[], retrieved=["a.md", "b.md"])
    assert result.retrieved == ["a.md", "b.md"]
