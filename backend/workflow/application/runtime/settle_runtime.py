"""Settle pipeline runtime factories (Lift E2 — resolver-backed).

Two production factories the :class:`SettleWorker` needs:

* :func:`build_settle_entity_extractor_factory` — per-settlement
  :class:`IngestCompiler` whose CompileLlm seam routes the extraction
  call through the resolver (caller_id
  :data:`backend.dispatch.caller_registry.CALLER_SETTLE_EXTRACT`). On
  miss (:class:`NoMatchingRouteError`) returns ``None`` so the sink
  soft-falls back to the deterministic heuristic.
* :func:`build_note_embed_hook` — unchanged from E1 (no LLM call, just
  the configured knowledge embedder).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings, get_settings
from backend.dispatch.caller_registry import CALLER_SETTLE_EXTRACT
from backend.knowledge.infrastructure.workers.settle_worker import (
    EntityExtractor,
    ExtractorFactory,
    NoteEmbedHook,
    Settlement,
)
from backend.workflow.application.runtime.account_resolution import _resolve_via_caller
from backend.workflow.application.runtime.dispatcher import _ResolverCompileLlm

logger = structlog.get_logger(__name__)


def build_settle_entity_extractor_factory(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
) -> ExtractorFactory:
    """Production :class:`ExtractorFactory` for the settle sink.

    Per call (one per settlement), resolves the workspace's account via
    caller_id ``workflow.settle.extract`` and builds an
    :class:`~backend.knowledge.ingest.ingest_compiler.IngestCompiler`
    rooted at the same ``<vault_root>/<region>/<workspace_id>/`` boundary
    the sink writes to. Returns ``None`` on
    :class:`~backend.dispatch.resolver.NoMatchingRouteError` so derived
    knowledge never silently routes to an unintended model.
    """
    settings = settings or get_settings()
    vault_root = Path(settings.knowledge_vault_root)

    async def _factory(*, region: str, workspace_id: uuid.UUID) -> EntityExtractor | None:
        from backend.knowledge.factory import KnowledgeFactory  # noqa: PLC0415 — lazy
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler  # noqa: PLC0415

        async with session_factory() as session:
            resolved = await _resolve_via_caller(
                session,
                caller_id=CALLER_SETTLE_EXTRACT,
                workspace_id=workspace_id,
                settings=settings,
            )
            if resolved is None:
                logger.info(
                    "settle_extractor_account_unresolved",
                    workspace_id=str(workspace_id),
                    caller_id=CALLER_SETTLE_EXTRACT,
                )
                return None
            llm = _ResolverCompileLlm(adapter=resolved.adapter)
            knowledge = KnowledgeFactory(
                region=region,
                workspace_id=str(workspace_id),
                vault_root=vault_root,
            )
            return IngestCompiler(garden_writer=knowledge.writer(), llm_client=llm)

    return _factory


def build_note_embed_hook(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
) -> NoteEmbedHook:
    """Production :class:`NoteEmbedHook` for the settle sink — unchanged from E1."""
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
    workspace_root = vault_root / region / str(workspace_id)
    try:
        return Path(node_ref).relative_to(workspace_root).as_posix()
    except ValueError:
        return node_ref


__all__ = [
    "build_note_embed_hook",
    "build_settle_entity_extractor_factory",
]
