"""Full knowledge-accumulation loop e2e — from an EMPTY slate.

Proves the proposal's core promise end-to-end against REAL infra: a settled
verified-work observation accumulates as knowledge across ALL layers —

1. Markdown garden note (the source of truth) written to the vault,
2. the pgvector ``note_embeddings`` index DERIVED from it (G6 — auto, from the
   deployment embedding model, no per-account opt-in), and
3. semantic retrieval finds it back.

Driven through the REAL :class:`SettleWorker` (drain → sink → embed hook), not a
hand-built path. Starts from a fresh workspace + tmp vault + the workspace's
``note_embeddings`` cleared, so every assertion is a delta from zero — knowledge
that wasn't there before this run.

Gated + real: needs Postgres (``BSVIBE_DATABASE_URL`` → dev PG with the
``note_embeddings`` table) + a live Ollama (``bge-m3``). Skipped otherwise — the
prod stack is never touched.

    BSVIBE_DATABASE_URL=postgresql+asyncpg://bsvibe:bsvibe@localhost:15442/bsvibe \
        uv run pytest tests/test_knowledge_accumulation_e2e.py -v -s
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.executors.db  # noqa: F401 — register tables on the shared Base
from backend.config import get_settings
from backend.execution.db import ExecutionRun, ExecutionRunActivity, RunStatus
from backend.knowledge.retrieval.embedder_resolution import resolve_knowledge_embedder
from backend.knowledge.retrieval.semantic_note_retriever import SemanticNoteRetriever
from backend.knowledge.retrieval.storage.pg import PgNoteVectorBackend
from backend.workers.run import build_note_embed_hook
from backend.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
    build_garden_promoter_factory,
)

pytestmark = pytest.mark.asyncio

_OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
_EMBED_MODEL = os.environ.get("BSVIBE_E2E_EMBED_MODEL", "ollama/bge-m3")
_REGION = "us-1"


async def _pg_url() -> str | None:
    url = os.environ.get("BSVIBE_DATABASE_URL")
    if not url:
        return None
    engine = create_async_engine(url, future=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM note_embeddings LIMIT 0"))
        return url
    except Exception:
        return None
    finally:
        await engine.dispose()


def _ollama_up() -> bool:
    try:
        return httpx.get(f"{_OLLAMA_BASE}/api/tags", timeout=2.0).status_code == 200
    except Exception:
        return False


async def _seed_settle_activity(sf, *, workspace_id: uuid.UUID, summary: str) -> None:
    async with sf() as s:
        run_id = uuid.uuid4()
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                request_id=uuid.uuid4(),
                status=RunStatus.REVIEW_READY,
                payload={"intent_text": "harden the payment checkout flow"},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            ExecutionRunActivity(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=workspace_id,
                activity_type="settle",
                payload={
                    "verified": True,
                    "artifact_refs": ["backend/payments/checkout.py"],
                    "summary": summary,
                    "intent_text": "harden the payment checkout flow",
                },
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()


async def test_full_knowledge_accumulation_from_empty_e2e(tmp_path: Path) -> None:
    url = await _pg_url()
    if url is None:
        pytest.skip("Postgres (with note_embeddings) not reachable — set BSVIBE_DATABASE_URL")
    if not _ollama_up():
        pytest.skip(f"Ollama not reachable at {_OLLAMA_BASE}")

    vault_root = tmp_path / "vault"
    settings = get_settings().model_copy(
        update={
            "knowledge_vault_root": str(vault_root),
            "knowledge_default_region": _REGION,
            "knowledge_embedding_model": _EMBED_MODEL,
            "knowledge_embedding_api_base": _OLLAMA_BASE,
            "knowledge_embedding_timeout_s": 30.0,
        }
    )
    workspace_id = uuid.uuid4()
    engine = create_async_engine(url, future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    summary = "Added a regression-test gate to the payment and billing checkout flow"

    try:
        # --- clean slate: this workspace has NO knowledge yet ---------------
        async with sf() as s:
            await s.execute(
                text("DELETE FROM note_embeddings WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            )
            await s.commit()
        assert _md_notes(vault_root, workspace_id) == []

        await _seed_settle_activity(sf, workspace_id=workspace_id, summary=summary)

        # --- drive the REAL settle worker: drain → garden note + embedding --
        worker = SettleWorker(
            session_factory=sf,
            sink=KnowledgeSettleSink(vault_root=vault_root),
            config=SettleWorkerConfig(default_region=_REGION),
            promoter_factory=build_garden_promoter_factory(vault_root=vault_root),
            embed_hook=build_note_embed_hook(session_factory=sf, settings=settings),
        )
        processed = await worker.drain_once()
        assert processed == 1

        # 1. Markdown garden note (the source of truth) accumulated.
        md = _md_notes(vault_root, workspace_id)
        garden = [p for p in md if "/garden/" in p.as_posix()]
        assert garden, f"no garden note written; vault: {md}"
        assert any(summary in p.read_text(encoding="utf-8") for p in garden)

        # 2. pgvector index DERIVED from it (auto, deployment model).
        async with sf() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT note_path, embedding_model FROM note_embeddings "
                            "WHERE workspace_id = :ws"
                        ),
                        {"ws": workspace_id},
                    )
                )
                .mappings()
                .all()
            )
        assert len(rows) == 1, rows
        assert rows[0]["embedding_model"] == _EMBED_MODEL
        assert "/garden/" in f"/{rows[0]['note_path']}"

        # 3. Semantic retrieval finds the freshly-accumulated knowledge.
        embedder = resolve_knowledge_embedder(settings)
        async with sf() as s:
            backend = PgNoteVectorBackend(
                s, workspace_id=workspace_id, embedding_model=_EMBED_MODEL
            )
            retriever = SemanticNoteRetriever(embedder, backend, top_k=3, min_similarity=0.3)
            hits = await retriever.retrieve_for_signals(
                "we're touching the billing checkout page — anything required?"
            )
        assert hits, "semantic search found nothing for the accumulated note"
        assert any("garden" in h for h in hits), hits
    finally:
        async with sf() as s:
            await s.execute(
                text("DELETE FROM note_embeddings WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            )
            await s.commit()
        await engine.dispose()


def _md_notes(vault_root: Path, workspace_id: uuid.UUID) -> list[Path]:
    ws_dir = vault_root / _REGION / str(workspace_id)
    return list(ws_dir.rglob("*.md")) if ws_dir.exists() else []
