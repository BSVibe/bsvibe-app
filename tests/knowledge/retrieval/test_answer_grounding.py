"""Answer grounding — a founder's question is answered from note CONTENT.

The retrievers were built for the VERIFY path, where a semantic hit only has to
identify a note: :class:`SemanticNoteRetriever` emits ``"Related note — <path>"``,
a pointer with no knowledge in it. Feeding that to an answer prompt hands the model
a filename and nothing to say — and the base retriever (promoted concepts + resolved
decisions) is empty on a young workspace, so the answer had NO grounding at all
(prod 2026-07-13: 330 notes in the vault, 0 statements retrieved; the model, left
with nothing, described its own empty sandbox instead).

:class:`AnswerGroundingRetriever` wraps any retriever and expands note refs into the
note's actual text. It is applied ONLY on the two answer paths — the verify path's
statements are untouched, so judge criteria keep their exact wire format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.knowledge.retrieval.answer_grounding import AnswerGroundingRetriever
from backend.knowledge.retrieval.knowledge_item import RetrievedKnowledge

pytestmark = pytest.mark.asyncio

_NOTE = """---
captured_at: '2026-07-13'
tags:
- intent-routing
type: TechInsight
---

# knowledge_only 요청을 executor LLM에 라우팅하면 크래시

프레임이 질문으로 분류한 요청은 chat 모델로 답해야 한다.
"""


class _StubInner:
    def __init__(self, items: list[RetrievedKnowledge]) -> None:
        self._items = items
        self.seen: list[str] = []

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        return [i.text for i in await self.retrieve_structured(signals)]

    async def retrieve_structured(self, signals: str) -> list[RetrievedKnowledge]:
        self.seen.append(signals)
        return list(self._items)


class _StubVault:
    def __init__(self, notes: dict[str, str]) -> None:
        self._notes = notes

    async def read_note_content(self, path: Any) -> str:
        try:
            return self._notes[str(path)]
        except KeyError as exc:
            raise FileNotFoundError(str(path)) from exc


def _note_item(path: str) -> RetrievedKnowledge:
    return RetrievedKnowledge(text=f"Related note — {path}", kind="note", ref=path, label=path)


async def test_note_ref_is_expanded_into_its_content() -> None:
    """The whole point: the model gets the note's KNOWLEDGE, not its filename."""
    path = "garden/seedling/knowledge_only-라우팅.md"
    inner = _StubInner([_note_item(path)])
    retriever = AnswerGroundingRetriever(inner, _StubVault({path: _NOTE}))

    statements = await retriever.retrieve_for_signals("현 프로젝트 상황 설명해줘")

    assert len(statements) == 1
    body = statements[0]
    assert "knowledge_only 요청을 executor LLM에 라우팅하면 크래시" in body
    assert "프레임이 질문으로 분류한 요청은 chat 모델로 답해야 한다." in body
    # The YAML frontmatter is metadata, not knowledge — it must not eat the budget.
    assert "captured_at" not in body
    assert "type: TechInsight" not in body


async def test_non_note_statements_pass_through_unchanged() -> None:
    """Concepts and prior decisions already carry their text — do not touch them
    (the verify path depends on this exact wire format)."""
    items = [
        RetrievedKnowledge(text="Idempotency — every write is keyed.", kind="concept", ref="c1"),
        RetrievedKnowledge(text="Prior decision — Q: ship? A: yes", kind="decision", ref="d1"),
    ]
    retriever = AnswerGroundingRetriever(_StubInner(items), _StubVault({}))

    assert await retriever.retrieve_for_signals("x") == [
        "Idempotency — every write is keyed.",
        "Prior decision — Q: ship? A: yes",
    ]


async def test_unreadable_note_falls_back_to_the_pointer() -> None:
    """A note that vanished (retracted, moved) must not crash or drop the answer —
    grounding degrades, it never breaks the reply."""
    inner = _StubInner([_note_item("garden/seedling/gone.md")])
    retriever = AnswerGroundingRetriever(inner, _StubVault({}))

    statements = await retriever.retrieve_for_signals("x")

    assert statements == ["Related note — garden/seedling/gone.md"]


async def test_content_is_capped(tmp_path: Path) -> None:
    """A long note cannot blow the answer prompt's budget."""
    path = "garden/seedling/long.md"
    inner = _StubInner([_note_item(path)])
    retriever = AnswerGroundingRetriever(
        inner, _StubVault({path: "# T\n\n" + ("x" * 5000)}), max_chars=500
    )

    statements = await retriever.retrieve_for_signals("x")

    assert len(statements[0]) <= 500


async def test_structured_items_keep_their_identity() -> None:
    """The expanded text rides on the SAME item — ref/kind/label are preserved so
    the report can still link the note it grounded the answer in."""
    path = "garden/seedling/knowledge_only-라우팅.md"
    inner = _StubInner([_note_item(path)])
    retriever = AnswerGroundingRetriever(inner, _StubVault({path: _NOTE}))

    items = await retriever.retrieve_structured("x")

    assert items[0].ref == path
    assert items[0].kind == "note"
    assert "chat 모델로 답해야 한다" in items[0].text
