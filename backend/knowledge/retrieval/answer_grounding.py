"""Answer grounding — give the answer paths note CONTENT, not note filenames.

The retrievers were built for the VERIFY path, where a semantic hit only has to
IDENTIFY a note: :class:`~backend.knowledge.retrieval.semantic_note_retriever.SemanticNoteRetriever`
emits ``"Related note — <path>"``, a pointer carrying no knowledge. That is right
for judge criteria (the report drops those hits) and useless for an answer: the
model receives filenames and has nothing to say.

Worse, on a young workspace the base retriever (promoted concepts + resolved
decisions) is legitimately empty, so a founder's question reached the model with
NO grounding at all — 330 notes in the vault, 0 statements retrieved (prod,
2026-07-13). Given nothing, the model described the only thing it could see: its
own empty sandbox.

:class:`AnswerGroundingRetriever` wraps any :class:`CanonRetriever` and expands
each note ref into the note's text. It is applied ONLY on the answer paths (the
inline ``/messages/ask`` service and :class:`KnowledgeAnswerOrchestrator`) — the
verify path keeps its exact statement wire format, so judge criteria are unchanged.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

import structlog

from backend.knowledge.retrieval.knowledge_item import RetrievedKnowledge

logger = structlog.get_logger(__name__)

#: Per-note budget. Enough for a seedling's insight; small enough that a handful
#: of notes cannot crowd out the founder's actual question in the prompt.
_DEFAULT_MAX_CHARS = 1200


class _Retriever(Protocol):
    async def retrieve_for_signals(self, signals: str) -> list[str]: ...
    async def retrieve_structured(self, signals: str) -> list[RetrievedKnowledge]: ...


class _NoteReader(Protocol):
    async def read_note_content(self, path: Any) -> str: ...


def _strip_frontmatter(raw: str) -> str:
    """Drop a leading YAML frontmatter block — it is metadata, not knowledge, and
    it would otherwise eat most of the per-note budget."""
    text = raw.lstrip()
    if not text.startswith("---"):
        return raw.strip()
    end = text.find("\n---", 3)
    if end == -1:
        return text.strip()
    return text[end + len("\n---") :].strip()


class AnswerGroundingRetriever:
    """Wrap a retriever so note hits carry their content into the answer prompt."""

    def __init__(
        self,
        inner: _Retriever,
        vault: _NoteReader,
        *,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> None:
        self._inner = inner
        self._vault = vault
        self._max_chars = max_chars

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        return [item.text for item in await self.retrieve_structured(signals)]

    async def retrieve_structured(self, signals: str) -> list[RetrievedKnowledge]:
        items = await self._inner.retrieve_structured(signals)
        return [await self._expand(item) for item in items]

    async def _expand(self, item: RetrievedKnowledge) -> RetrievedKnowledge:
        """Replace a note pointer with the note's text, keeping its identity.

        A note that cannot be read (retracted, moved, permission) degrades to the
        pointer it already was — grounding degrades, the answer never breaks."""
        if item.kind != "note" or not item.ref:
            return item
        try:
            raw = await self._vault.read_note_content(item.ref)
        except Exception:  # noqa: BLE001 — grounding must never crash the answer
            logger.warning("answer_grounding_note_unreadable", ref=item.ref)
            return item
        body = _strip_frontmatter(raw)
        if not body:
            return item
        return RetrievedKnowledge(
            text=body[: self._max_chars],
            kind=item.kind,
            ref=item.ref,
            label=item.label,
        )


def build_answer_retriever(session: Any, *, settings: Any, workspace_id: uuid.UUID) -> Any:
    """The retriever an ANSWER should use: canon + semantic note search, with note
    hits expanded to their content.

    The single builder both answer paths share. They used to differ — the async
    :class:`KnowledgeAnswerOrchestrator` composed semantic note search while the
    inline ``/messages/ask`` service used the canon retriever alone, so the same
    question was grounded differently depending on which surface the founder asked
    from (and the inline one, on a workspace with no promoted concepts yet, was
    grounded in nothing at all).
    """
    from pathlib import Path  # noqa: PLC0415

    from backend.knowledge.factory import KnowledgeFactory  # noqa: PLC0415
    from backend.knowledge.retrieval.composite_retriever import (  # noqa: PLC0415
        CompositeCanonRetriever,
    )
    from backend.knowledge.retrieval.embedder_resolution import (  # noqa: PLC0415
        resolve_knowledge_embedder,
    )
    from backend.knowledge.retrieval.semantic_note_retriever import (  # noqa: PLC0415
        SemanticNoteRetriever,
    )
    from backend.knowledge.retrieval.storage.pg import PgNoteVectorBackend  # noqa: PLC0415

    factory = KnowledgeFactory(
        region=settings.knowledge_default_region,
        workspace_id=str(workspace_id),
        vault_root=Path(settings.knowledge_vault_root),
    )
    base = factory.retriever()
    embedder = resolve_knowledge_embedder(settings)
    if not embedder.enabled or embedder.model is None:
        inner: Any = base
    else:
        semantic = SemanticNoteRetriever(
            embedder,
            PgNoteVectorBackend(session, workspace_id=workspace_id, embedding_model=embedder.model),
        )
        inner = CompositeCanonRetriever([base, semantic])
    return AnswerGroundingRetriever(inner, factory.vault())


__all__ = ["AnswerGroundingRetriever", "build_answer_retriever"]
