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

import uuid
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


def _vault(tmp_path: Path, notes: dict[str, str]) -> tuple[Any, Path]:
    """A REAL :class:`Vault` over ``tmp_path``.

    Deliberately not a stub: ``Vault.read_note_content`` takes an ABSOLUTE path and
    rejects anything resolving outside the vault root. A dict-stub keyed by the
    vault-relative ref happily accepted what the retriever passed and hid the
    mismatch — live prod then logged ``answer_grounding_note_unreadable`` for every
    hit and the answer was ungrounded again. Test against the real contract.
    """
    from backend.knowledge.graph.vault import Vault

    for rel, body in notes.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return Vault(tmp_path), tmp_path


def _note_item(path: str) -> RetrievedKnowledge:
    return RetrievedKnowledge(text=f"Related note — {path}", kind="note", ref=path, label=path)


async def test_note_ref_is_expanded_into_its_content(tmp_path: Path) -> None:
    """The whole point: the model gets the note's KNOWLEDGE, not its filename.

    The ref is vault-RELATIVE ("garden/seedling/x.md"); the vault reads by ABSOLUTE
    path. Resolving that is the retriever's job — get it wrong and every hit is
    silently unreadable (prod, 2026-07-13)."""
    path = "garden/seedling/knowledge_only-라우팅.md"
    vault, root = _vault(tmp_path, {path: _NOTE})
    inner = _StubInner([_note_item(path)])
    retriever = AnswerGroundingRetriever(inner, vault, root=root)

    statements = await retriever.retrieve_for_signals("현 프로젝트 상황 설명해줘")

    assert len(statements) == 1
    body = statements[0]
    assert "knowledge_only 요청을 executor LLM에 라우팅하면 크래시" in body
    assert "프레임이 질문으로 분류한 요청은 chat 모델로 답해야 한다." in body
    # The YAML frontmatter is metadata, not knowledge — it must not eat the budget.
    assert "captured_at" not in body
    assert "type: TechInsight" not in body


async def test_non_note_statements_pass_through_unchanged(tmp_path: Path) -> None:
    """Concepts and prior decisions already carry their text — do not touch them
    (the verify path depends on this exact wire format)."""
    items = [
        RetrievedKnowledge(text="Idempotency — every write is keyed.", kind="concept", ref="c1"),
        RetrievedKnowledge(text="Prior decision — Q: ship? A: yes", kind="decision", ref="d1"),
    ]
    vault, root = _vault(tmp_path, {})
    retriever = AnswerGroundingRetriever(_StubInner(items), vault, root=root)

    assert await retriever.retrieve_for_signals("x") == [
        "Idempotency — every write is keyed.",
        "Prior decision — Q: ship? A: yes",
    ]


async def test_unreadable_note_falls_back_to_the_pointer(tmp_path: Path) -> None:
    """A note that vanished (retracted, moved) must not crash or drop the answer —
    grounding degrades, it never breaks the reply."""
    vault, root = _vault(tmp_path, {})
    inner = _StubInner([_note_item("garden/seedling/gone.md")])
    retriever = AnswerGroundingRetriever(inner, vault, root=root)

    statements = await retriever.retrieve_for_signals("x")

    assert statements == ["Related note — garden/seedling/gone.md"]


async def test_content_is_capped(tmp_path: Path) -> None:
    """A long note cannot blow the answer prompt's budget."""
    path = "garden/seedling/long.md"
    vault, root = _vault(tmp_path, {path: "# T\n\n" + ("x" * 5000)})
    inner = _StubInner([_note_item(path)])
    retriever = AnswerGroundingRetriever(inner, vault, root=root, max_chars=500)

    statements = await retriever.retrieve_for_signals("x")

    assert len(statements[0]) <= 500


async def test_structured_items_keep_their_identity(tmp_path: Path) -> None:
    """The expanded text rides on the SAME item — ref/kind/label are preserved so
    the report can still link the note it grounded the answer in."""
    path = "garden/seedling/knowledge_only-라우팅.md"
    vault, root = _vault(tmp_path, {path: _NOTE})
    inner = _StubInner([_note_item(path)])
    retriever = AnswerGroundingRetriever(inner, vault, root=root)

    items = await retriever.retrieve_structured("x")

    assert items[0].ref == path
    assert items[0].kind == "note"
    assert "chat 모델로 답해야 한다" in items[0].text


# ── the builder itself — the seam both answer paths actually call ────────────
#
# Twice now a defect slipped past the unit tests above because they construct
# AnswerGroundingRetriever by hand: the vault-relative/absolute path mismatch, and
# ``factory.vault_path()`` (a PROPERTY, called like a method). Exercise the real
# builder against a real vault so the wiring itself is covered.


async def test_build_answer_retriever_reads_real_notes(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from backend.knowledge.retrieval import answer_grounding as ag

    region, ws = "us-1", uuid.uuid4()
    note = "garden/seedling/routing.md"
    vault_root = tmp_path / region / str(ws)
    (vault_root / "garden" / "seedling").mkdir(parents=True)
    (vault_root / note).write_text(_NOTE, encoding="utf-8")

    # No embedder configured → the canon retriever alone; the note still has to be
    # readable through the wrapper, which is what these two bugs broke.
    monkeypatch.setattr(ag, "_DEFAULT_MAX_CHARS", 1200, raising=False)
    settings = SimpleNamespace(
        knowledge_default_region=region,
        knowledge_vault_root=str(tmp_path),
        knowledge_embedding_model=None,
    )

    retriever = ag.build_answer_retriever(None, settings=settings, workspace_id=ws)

    # The wrapper resolves refs against the vault root — prove it on a real note.
    expanded = await retriever._expand(
        RetrievedKnowledge(text=f"Related note — {note}", kind="note", ref=note, label=note)
    )
    assert "chat 모델로 답해야 한다" in expanded.text
