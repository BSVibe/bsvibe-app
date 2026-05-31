"""Real end-to-end: pgvector note semantic search over a live Ollama embedder.

Exercises the G3/G5 stack against REAL infra — the part SQLite + fake-embedder
unit tests can't cover: real Ollama embeddings + pgvector's ``<=>`` cosine
operator through the production code path
(:class:`EmbeddingService` → :class:`GatewayEmbedder` →
:class:`PgNoteVectorBackend` → :class:`SemanticNoteRetriever`).

Skipped automatically when Postgres (``BSVIBE_DATABASE_URL``) or Ollama
(``OLLAMA_BASE_URL``, default ``http://localhost:11434``) is unreachable, so the
broad unit suite + CI (no Ollama) stay green. Run locally with a live stack:

    BSVIBE_DATABASE_URL=postgresql+asyncpg://bsvibe:bsvibe@localhost:15442/bsvibe \
        uv run pytest tests/test_semantic_search_e2e.py -v

Requires the ``note_embeddings`` table (``alembic upgrade head``) + an Ollama
embedding model (``bge-m3``).
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.knowledge.retrieval.embedder_adapter import GatewayEmbedder
from backend.knowledge.retrieval.semantic_note_retriever import SemanticNoteRetriever
from backend.knowledge.retrieval.storage.pg import PgNoteVectorBackend
from backend.router.embedding.provider import LiteLLMEmbeddingProvider
from backend.router.embedding.service import EmbeddingService
from backend.router.embedding.settings import EmbeddingSettings

pytestmark = pytest.mark.asyncio

_OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
_EMBED_MODEL = os.environ.get("BSVIBE_E2E_EMBED_MODEL", "ollama/bge-m3")


async def _pg_url() -> str | None:
    url = os.environ.get("BSVIBE_DATABASE_URL")
    if not url:
        return None
    engine = create_async_engine(url, future=True)
    try:
        async with engine.connect() as conn:
            # Require the note_embeddings table — proves migrations are applied.
            await conn.execute(text("SELECT 1 FROM note_embeddings LIMIT 0"))
        return url
    except Exception:
        return None
    finally:
        await engine.dispose()


def _ollama_up() -> bool:
    try:
        r = httpx.get(f"{_OLLAMA_BASE}/api/tags", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def _embedder() -> GatewayEmbedder:
    settings = EmbeddingSettings(model=_EMBED_MODEL, api_base=_OLLAMA_BASE, timeout=30.0)
    return GatewayEmbedder(EmbeddingService(LiteLLMEmbeddingProvider(settings)))


def _deployment_settings(tmp_vault: str):
    """App Settings with the knowledge embedding model configured at the
    DEPLOYMENT level (G6) — the derived-index switch the prod hook reads."""
    from backend.config import get_settings

    return get_settings().model_copy(
        update={
            "knowledge_embedding_model": _EMBED_MODEL,
            "knowledge_embedding_api_base": _OLLAMA_BASE,
            "knowledge_embedding_timeout_s": 30.0,
            "knowledge_vault_root": tmp_vault,
        }
    )


async def test_semantic_note_search_e2e() -> None:
    url = await _pg_url()
    if url is None:
        pytest.skip("Postgres (with note_embeddings) not reachable — set BSVIBE_DATABASE_URL")
    if not _ollama_up():
        pytest.skip(f"Ollama not reachable at {_OLLAMA_BASE}")

    embedder = _embedder()
    workspace_id = uuid.uuid4()
    engine = create_async_engine(url, future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sf() as session:
            backend = PgNoteVectorBackend(
                session, workspace_id=workspace_id, embedding_model=embedder.model or _EMBED_MODEL
            )
            # Populate three real notes via the production embed path.
            notes = {
                "garden/payments.md": (
                    "Always run the payment regression suite before changing the "
                    "checkout or billing screens."
                ),
                "garden/onboarding.md": (
                    "The new-user onboarding flow should send a welcome email and "
                    "create a starter workspace."
                ),
                "garden/infra.md": (
                    "Rotate the nginx TLS certificates and reload the load balancer "
                    "every ninety days."
                ),
            }
            for path, body in notes.items():
                vector = await embedder.embed(body)
                assert vector, f"embedding came back empty for {path}"
                await backend.store(path, vector)
            await session.commit()

            # Search with a query semantically near the payments note.
            retriever = SemanticNoteRetriever(embedder, backend, top_k=3, min_similarity=0.3)
            hits = await retriever.retrieve_for_signals(
                "we're updating the billing settings page — what should we watch for?"
            )
            assert hits, "semantic search returned nothing"
            # The payments note must be the top related note (real cosine ranking).
            assert "garden/payments.md" in hits[0], hits
            # The unrelated infra note must NOT outrank payments.
            assert not (hits and "garden/infra.md" in hits[0]), hits

            # Direct backend ranking sanity: payments first, infra last.
            qvec = await embedder.embed("billing and checkout payment changes")
            ranked = await backend.search(qvec, top_k=3)
            paths = [p for p, _ in ranked]
            assert paths[0] == "garden/payments.md", ranked
            assert paths[-1] == "garden/infra.md", ranked
        # Cleanup — drop this workspace's e2e rows.
        async with sf() as session:
            await session.execute(
                text("DELETE FROM note_embeddings WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            )
            await session.commit()
    finally:
        await engine.dispose()


async def test_settle_hook_auto_populates_pgvector_index_e2e(tmp_path) -> None:
    """G6 derived-index: the PROD settle hook (build_note_embed_hook), with only
    a DEPLOYMENT-level knowledge embedding model set (no per-account opt-in),
    embeds a settled note into note_embeddings — and the same-settings retriever
    finds it. Proves search auto-accumulates in pgvector alongside the md SoT."""
    import uuid as _uuid
    from datetime import UTC, datetime

    from backend.knowledge.retrieval.embedder_resolution import resolve_knowledge_embedder
    from backend.workers.run import build_note_embed_hook
    from backend.workers.settle_worker import Settlement

    url = await _pg_url()
    if url is None:
        pytest.skip("Postgres (with note_embeddings) not reachable — set BSVIBE_DATABASE_URL")
    if not _ollama_up():
        pytest.skip(f"Ollama not reachable at {_OLLAMA_BASE}")

    settings = _deployment_settings(str(tmp_path / "vault"))
    workspace_id = _uuid.uuid4()
    engine = create_async_engine(url, future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        hook = build_note_embed_hook(session_factory=sf, settings=settings)
        # A settled note (the hook embeds the summary; node_ref keys the row).
        node_ref = f"{settings.knowledge_vault_root}/us-1/{workspace_id}/garden/seedling/pay.md"
        settlement = Settlement(
            workspace_id=workspace_id,
            region="us-1",
            run_id=_uuid.uuid4(),
            activity_id=_uuid.uuid4(),
            verified=True,
            summary="never ship a payment or billing change without a regression test",
            occurred_at=datetime.now(tz=UTC),
        )
        await hook(settlement, node_ref)

        # The note auto-landed in the pgvector index (derived from the md SoT).
        async with sf() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT note_path, embedding_model, dimension FROM note_embeddings "
                            "WHERE workspace_id = :ws"
                        ),
                        {"ws": workspace_id},
                    )
                )
                .mappings()
                .all()
            )
        assert len(row) == 1, row
        assert row[0]["note_path"] == "garden/seedling/pay.md"
        assert row[0]["embedding_model"] == _EMBED_MODEL
        assert row[0]["dimension"] > 0

        # And it's findable by the same-settings retriever (the consumption seam).
        embedder = resolve_knowledge_embedder(settings)
        async with sf() as session:
            backend = PgNoteVectorBackend(
                session, workspace_id=workspace_id, embedding_model=_EMBED_MODEL
            )
            retriever = SemanticNoteRetriever(embedder, backend, top_k=3, min_similarity=0.3)
            hits = await retriever.retrieve_for_signals(
                "we're changing the billing settings page — anything to watch for?"
            )
        assert any("garden/seedling/pay.md" in h for h in hits), hits
    finally:
        async with sf() as session:
            await session.execute(
                text("DELETE FROM note_embeddings WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            )
            await session.commit()
        await engine.dispose()
