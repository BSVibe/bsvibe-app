"""Settle pipeline runtime factories (§17.2a slice).

Two production factories the :class:`SettleWorker` needs:

* :func:`build_settle_entity_extractor_factory` — per-settlement
  :class:`IngestCompiler` whose CompileLlm seam routes the extraction call
  through the SAME :class:`GatewayDispatcher` the chat/agent paths use.
  Resolution is the "exactly one active non-executor account → use it" policy;
  ZERO/MANY (or no LLM) returns None so the sink soft-falls back to the
  deterministic heuristic.
* :func:`build_note_embed_hook` — per absorbed note, embeds the note summary
  with the DEPLOYMENT knowledge embedder and upserts the vector into
  ``note_embeddings`` (pgvector) keyed by the note's vault-relative path.

Both extract concepts from EXTRACTED ENTITIES (BSage's mechanism) rather than
by tokenizing the work summary — so they live together.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings, get_settings
from backend.knowledge.infrastructure.workers.settle_worker import (
    EntityExtractor,
    ExtractorFactory,
    NoteEmbedHook,
    Settlement,
)
from backend.workflow.application.runtime.account_resolution import (
    _list_active_workspace_accounts,
    _single_native_account,
)
from backend.workflow.application.runtime.dispatcher import (
    _GatewayCompileLlm,
    build_gateway_dispatcher,
)

logger = structlog.get_logger(__name__)


def build_settle_entity_extractor_factory(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
) -> ExtractorFactory:
    """Production :class:`ExtractorFactory` for the settle sink.

    Per call (one per settlement), resolves the workspace's single active
    ModelAccount and builds an
    :class:`~backend.knowledge.ingest.ingest_compiler.IngestCompiler` rooted at
    the SAME ``<vault_root>/<region>/<workspace_id>/`` boundary the sink writes
    to, with a :class:`_GatewayCompileLlm` seam over a per-session
    :class:`GatewayDispatcher`. Returns ``None`` (soft-fallback) when the
    workspace has zero or more-than-one active account — never guessing a
    model for derived knowledge."""
    settings = settings or get_settings()
    vault_root = Path(settings.knowledge_vault_root)

    async def _factory(*, region: str, workspace_id: uuid.UUID) -> EntityExtractor | None:
        from backend.knowledge.factory import KnowledgeFactory  # noqa: PLC0415 — lazy heavy import
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler  # noqa: PLC0415

        async with session_factory() as session:
            accounts = await _list_active_workspace_accounts(session, workspace_id)
            account = _single_native_account(accounts)
            if account is None:
                logger.info(
                    "settle_extractor_account_unresolved",
                    workspace_id=str(workspace_id),
                    active_count=len(accounts),
                )
                return None
            dispatcher = build_gateway_dispatcher(session, settings)
            llm = _GatewayCompileLlm(
                dispatcher=dispatcher,
                workspace_id=workspace_id,
                account_id=account.account_id,
                model_account_id=account.id,
            )
            knowledge = KnowledgeFactory(
                region=region,
                workspace_id=str(workspace_id),
                vault_root=vault_root,
            )
            # retriever omitted (None) — entity extraction needs no vault
            # context; the compiler only extracts names from the seed text,
            # never writes.
            return IngestCompiler(garden_writer=knowledge.writer(), llm_client=llm)

    return _factory


def build_note_embed_hook(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
) -> NoteEmbedHook:
    """Production :class:`NoteEmbedHook` for the settle sink (G5b + G6).

    Per absorbed note, embeds the note summary with the DEPLOYMENT knowledge
    embedder (``settings.knowledge_embedding_model`` — G6, not per-account) and
    upserts the vector into ``note_embeddings`` (pgvector) keyed by the note's
    vault-relative path, so :class:`SemanticNoteRetriever` can find it — the
    pgvector index is the DERIVED index of the Markdown SoT (proposal §5.4).
    Opens its OWN session + commit (decoupled from the settle transaction).
    No-op when no knowledge embedding model is configured — the index simply
    isn't built rather than erroring."""
    settings = settings or get_settings()
    vault_root = Path(settings.knowledge_vault_root)

    async def _hook(settlement: Settlement, node_ref: str) -> None:
        from backend.knowledge.retrieval.embedder_resolution import (  # noqa: PLC0415
            resolve_knowledge_embedder,
        )
        from backend.knowledge.retrieval.storage.pg import (  # noqa: PLC0415
            PgNoteVectorBackend,
        )

        text = settlement.summary.strip()
        if not text:
            return
        embedder = resolve_knowledge_embedder(settings)
        if not embedder.enabled or embedder.model is None:
            return
        vector = await embedder.embed(text)
        if not vector:
            return
        note_path = _relative_note_path(
            node_ref, vault_root, settlement.region, settlement.workspace_id
        )
        async with session_factory() as session:
            backend = PgNoteVectorBackend(
                session,
                workspace_id=settlement.workspace_id,
                embedding_model=embedder.model,
            )
            await backend.store(note_path, vector)
            await session.commit()

    return _hook


def _relative_note_path(
    node_ref: str, vault_root: Path, region: str, workspace_id: uuid.UUID
) -> str:
    """The note's vault-relative path (e.g. ``garden/seedling/x.md``) for the
    embedding key, so it matches how the other retrievers reference notes.
    Falls back to the raw ``node_ref`` when it isn't under the workspace root
    (defensive — never raises)."""
    workspace_root = vault_root / region / str(workspace_id)
    try:
        return Path(node_ref).relative_to(workspace_root).as_posix()
    except ValueError:
        return node_ref


__all__ = [
    "build_note_embed_hook",
    "build_settle_entity_extractor_factory",
]
