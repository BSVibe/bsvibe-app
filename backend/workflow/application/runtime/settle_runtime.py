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
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings, get_settings
from backend.dispatch.caller_registry import CALLER_SETTLE_EXTRACT
from backend.knowledge.infrastructure.workers.settle_worker import (
    EntityExtractor,
    ExtractorFactory,
    NoteEmbedHook,
    ReconcileHook,
    Settlement,
)
from backend.workflow.application.runtime.account_resolution import _resolve_via_caller
from backend.workflow.application.runtime.dispatcher import _ResolverCompileLlm

logger = structlog.get_logger(__name__)


def build_settle_entity_extractor_factory(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
    redis: Any = None,
) -> ExtractorFactory:
    """Production :class:`ExtractorFactory` for the settle sink.

    Per call (one per settlement), resolves the workspace's account via
    caller_id ``workflow.settle.extract`` and builds an
    :class:`~backend.knowledge.ingest.ingest_compiler.IngestCompiler`
    rooted at the same ``<vault_root>/<region>/<workspace_id>/`` boundary
    the sink writes to. Returns ``None`` on
    :class:`~backend.dispatch.resolver.NoMatchingRouteError` so derived
    knowledge never silently routes to an unintended model.

    ``redis`` is threaded into the resolver so a settle that resolves to an
    EXECUTOR account (e.g. the workspace default is a claude_code/codex/opencode
    worker) can dispatch the entity-extraction chat onto the worker stream.
    Without it ``ExecutorAdapter.chat`` raises ``ExecutorAdapterUnavailable`` on
    every settle, the sink degrades to the deterministic tokenizer, and the
    promoter auto-promotes the resulting intent/summary words into noise
    concepts. ``None`` is fine for workspaces whose settle route is a LiteLLM
    account (they never touch the worker stream).
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
                # An executor-account settle route needs a Redis client to
                # dispatch the extraction chat onto the worker stream; without
                # it the ExecutorAdapter raises and the sink degrades to noise.
                redis=redis,
                # Lift E19 — same E18 race the bootstrap runtime hit. The
                # settle path also passes its IngestCompiler through
                # parallel chunks; each must own its own session for the
                # ExecutorAdapter dispatch lifecycle.
                session_factory=session_factory,
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
            return IngestCompiler(
                garden_writer=knowledge.writer(),
                llm_client=llm,
                parallelism=settings.ingest_compile_parallelism,
            )

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


def build_reconcile_hook(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
) -> ReconcileHook:
    """Production :class:`ReconcileHook` — embed a workspace's un-embedded
    knowledge notes after a concept-creating promote pass (Lift 2).

    Reuses :func:`~backend.knowledge.retrieval.reconcile.reconcile_embeddings`
    over the SAME ``<vault_root>/<region>/<workspace_id>/`` boundary the sink +
    promoter operate on. The reconcile is idempotent and model-aware (it diffs
    against ``existing_paths`` under the current model), so the marginal cost is
    reading the gap — in steady state just the freshly created concept. Owns its
    own session + commit; no-op when no embedding model is configured."""
    settings = settings or get_settings()
    vault_root = Path(settings.knowledge_vault_root)

    async def _hook(*, region: str, workspace_id: uuid.UUID) -> object:
        from backend.knowledge.graph.vault import Vault  # noqa: PLC0415
        from backend.knowledge.retrieval.embedder_resolution import (  # noqa: PLC0415
            resolve_knowledge_embedder,
        )
        from backend.knowledge.retrieval.reconcile import (  # noqa: PLC0415
            reconcile_embeddings,
        )
        from backend.knowledge.retrieval.storage.pg import (  # noqa: PLC0415
            PgNoteVectorBackend,
        )

        embedder = resolve_knowledge_embedder(settings)
        if not embedder.enabled or embedder.model is None:
            return None
        vault = Vault(vault_root / region / str(workspace_id))
        async with session_factory() as session:
            backend = PgNoteVectorBackend(
                session,
                workspace_id=workspace_id,
                embedding_model=embedder.model,
            )
            result = await reconcile_embeddings(vault, embedder, backend)
            await session.commit()
            return result

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
    "build_reconcile_hook",
    "build_settle_entity_extractor_factory",
]
