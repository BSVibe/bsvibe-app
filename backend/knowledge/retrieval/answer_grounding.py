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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from backend.knowledge.retrieval.knowledge_item import RetrievedKnowledge

if TYPE_CHECKING:
    from backend.workflow.application.verification_service import CanonRetriever

logger = structlog.get_logger(__name__)

#: Per-note budget. Enough for a seedling's insight; small enough that a handful
#: of notes cannot crowd out the founder's actual question in the prompt.
_DEFAULT_MAX_CHARS = 1200


class _Retriever(Protocol):
    async def retrieve_for_signals(self, signals: str) -> list[str]: ...
    async def retrieve_structured(self, signals: str) -> list[RetrievedKnowledge]: ...


class _NoteReader(Protocol):
    #: NOTE the contract: an ABSOLUTE path, inside the vault root. ``Vault`` resolves
    #: it and rejects anything escaping the boundary — a vault-relative ref resolves
    #: against the PROCESS cwd and is refused, so every hit reads back as unreadable.
    async def read_note_content(self, path: Path) -> str: ...


def _is_retracted(raw: str) -> bool:
    """Does the note carry a retraction tombstone (``retracted_at``) in its
    frontmatter? Retraction rewrites the note in place; the embedding index does not
    forget it, so every READER has to check."""
    text = raw.lstrip()
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    frontmatter = text[:end] if end != -1 else text
    return "retracted_at:" in frontmatter


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
        root: Path,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> None:
        self._inner = inner
        self._vault = vault
        # A retrieved ``ref`` is vault-RELATIVE ("garden/seedling/x.md"); the vault
        # reads by absolute path. Resolving the two is this class's job.
        self._root = root
        self._max_chars = max_chars

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        return [item.text for item in await self.retrieve_structured(signals)]

    async def retrieve_structured(self, signals: str) -> list[RetrievedKnowledge]:
        items = await self._inner.retrieve_structured(signals)
        expanded = [await self._expand(item) for item in items]
        return [item for item in expanded if item is not None]

    async def _expand(self, item: RetrievedKnowledge) -> RetrievedKnowledge | None:
        """Replace a note pointer with the note's text, keeping its identity.

        ``None`` drops the item: a RETRACTED note must never ground an answer. The
        tombstone lives in the note's frontmatter, but the embedding index keeps its
        row, so semantic search still returns retracted notes — the founder then gets
        their own retracted knowledge quoted back as fact (prod, 2026-07-13).
        Retraction has to be honoured at every consumer, not just at the writer.

        A note that cannot be read (moved, deleted, permission) degrades to the
        pointer it already was — grounding degrades, the answer never breaks."""
        if item.kind != "note" or not item.ref:
            return item
        try:
            raw = await self._vault.read_note_content(self._root / item.ref)
        except Exception:  # noqa: BLE001 — grounding must never crash the answer
            logger.warning("answer_grounding_note_unreadable", ref=item.ref)
            return item
        if _is_retracted(raw):
            logger.info("answer_grounding_note_retracted", ref=item.ref)
            return None
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
    from backend.knowledge.factory import KnowledgeFactory  # noqa: PLC0415

    factory = KnowledgeFactory(
        region=settings.knowledge_default_region,
        workspace_id=str(workspace_id),
        vault_root=Path(settings.knowledge_vault_root),
    )
    inner = build_canon_retriever(session, settings=settings, workspace_id=workspace_id)
    return AnswerGroundingRetriever(inner, factory.vault(), root=factory.vault_path)


def build_canon_retriever(
    session: Any, *, settings: Any, workspace_id: uuid.UUID
) -> CanonRetriever:
    """The canon retriever a RUN consults: promoted concepts + resolved decisions, with semantic
    note search folded in when a knowledge embedding model is configured.

    The single builder for the ``knowledge_search`` grounding on BOTH transports — the in-process
    loop (``build_agent_execution_deps._retriever_for``) and the MCP transport
    (``build_run_tool_registry``). Extracted so those two paths cannot ground the executor's
    ``knowledge_search`` differently. Unlike :func:`build_answer_retriever`, it does NOT expand
    note refs to content: mid-run search wants the compact statement wire format, not full notes.
    """
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

    base = KnowledgeFactory(
        region=settings.knowledge_default_region,
        workspace_id=str(workspace_id),
        vault_root=Path(settings.knowledge_vault_root),
    ).retriever()
    embedder = resolve_knowledge_embedder(settings)
    if not embedder.enabled or embedder.model is None:
        return base
    semantic = SemanticNoteRetriever(
        embedder,
        PgNoteVectorBackend(session, workspace_id=workspace_id, embedding_model=embedder.model),
    )
    return CompositeCanonRetriever([base, semantic])


__all__ = ["AnswerGroundingRetriever", "build_answer_retriever", "build_canon_retriever"]
