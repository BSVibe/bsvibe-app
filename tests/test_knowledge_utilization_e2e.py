"""Knowledge UTILIZATION loop e2e — accumulated knowledge is USED by verify.

The accumulation e2e proves knowledge lands (md + canon + pgvector). This proves
the other half of the proposal's promise: a LATER task actually *uses* it. It
drives the SAME retriever the production verify (B3) + work-start seed (B6)
consume (``_retriever_for`` composition: canon + resolved-decisions + negative
+ G6 semantic note search) and asserts the accumulated knowledge is folded into
the run's verification contract as judge criteria — i.e. the verify gate reasons
against past knowledge, it isn't just stored.

Two phases, real components (dev Postgres + live Ollama bge-m3):
1. ACCUMULATE — a verified-work note settles (→ md garden note + pgvector
   embedding) and a prior resolved decision is on record.
2. USE — for a NEW change whose signals overlap, ``VerificationService.assemble_contract``
   folds the retrieved knowledge (the semantic "Related note …" + the "Prior
   decision …") into the contract the judge will reason against.

Gated on ``BSVIBE_DATABASE_URL`` (dev PG with note_embeddings) + Ollama; skipped
otherwise — prod is never touched.

    BSVIBE_DATABASE_URL=postgresql+asyncpg://bsvibe:bsvibe@localhost:15442/bsvibe \
        uv run pytest tests/test_knowledge_utilization_e2e.py -v -s
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
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenNote
from backend.knowledge.graph.writer_core import GardenWriter
from backend.knowledge.infrastructure.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
    build_garden_promoter_factory,
)
from backend.knowledge.retrieval.composite_retriever import CompositeCanonRetriever
from backend.knowledge.retrieval.embedder_resolution import resolve_knowledge_embedder
from backend.knowledge.retrieval.semantic_note_retriever import SemanticNoteRetriever
from backend.knowledge.retrieval.storage.pg import PgNoteVectorBackend
from backend.workflow.application.verification_service import (
    RETRIEVED_KNOWLEDGE_RATIONALE,
    VerificationService,
)
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunActivity, RunStatus
from backend.workflow.infrastructure.workers.run import build_note_embed_hook

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


class _NoopJudge:
    """assemble_contract never calls the judge; verify() would. Satisfies the ctor."""

    async def complete(self, *, messages, tools):  # pragma: no cover - unused here
        raise AssertionError("judge should not be called by assemble_contract")


async def _seed_settle(sf, *, workspace_id: uuid.UUID, summary: str) -> None:
    async with sf() as s:
        run_id = uuid.uuid4()
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                request_id=uuid.uuid4(),
                status=RunStatus.REVIEW_READY,
                payload={"intent_text": "harden the search API"},
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
                    "artifact_refs": ["backend/api/search.py"],
                    "summary": summary,
                    "intent_text": "harden the search API",
                },
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()


def _seed_prior_decision(vault_root: Path, workspace_id: uuid.UUID, *, question: str, answer: str):
    root = vault_root / _REGION / str(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    writer = GardenWriter(vault=Vault(root))
    summary = f"Decision resolved — Q: {question} A: {answer}"
    return writer.write_garden(
        GardenNote(
            title=f"Settle: {summary[:80]}",
            content=summary,
            source="settle_worker",
            knowledge_layer="episodic",
            tags=["settle", "verified-run", "decision-resolution"],
            extra_fields={"kind": "decision_resolution", "question": question, "answer": answer},
        )
    )


def _production_retriever(session, *, settings, workspace_id: uuid.UUID):
    """Reproduces ``backend.workflow.infrastructure.workers.run._retriever_for`` (the retriever verify +
    seed use): canon + resolved-decisions + negative + G6 semantic note search."""
    from backend.knowledge.factory import KnowledgeFactory

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


async def test_accumulated_knowledge_is_used_by_verify_e2e(tmp_path: Path) -> None:
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

    try:
        # --- clean slate for this workspace --------------------------------
        async with sf() as s:
            await s.execute(
                text("DELETE FROM note_embeddings WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            )
            await s.commit()

        # === PHASE 1: ACCUMULATE ==========================================
        await _seed_settle(
            sf,
            workspace_id=workspace_id,
            summary="Verified: added a rate limiter to the public search API endpoint with tests",
        )
        await _seed_prior_decision(
            vault_root,
            workspace_id,
            question="Should the search endpoint enforce a rate limit?",
            answer="Yes — token-bucket, 10 requests/second per API key",
        )
        from tests._support import always_remember_extractor_factory  # noqa: PLC0415

        worker = SettleWorker(
            session_factory=sf,
            sink=KnowledgeSettleSink(
                vault_root=vault_root,
                memory_extractor=always_remember_extractor_factory(),
            ),
            config=SettleWorkerConfig(default_region=_REGION),
            promoter_factory=build_garden_promoter_factory(vault_root=vault_root),
            embed_hook=build_note_embed_hook(session_factory=sf, settings=settings),
        )
        assert await worker.drain_once() == 1
        # the note embedded into pgvector (accumulation precondition)
        async with sf() as s:
            n = (
                await s.execute(
                    text("SELECT count(*) FROM note_embeddings WHERE workspace_id = :ws"),
                    {"ws": workspace_id},
                )
            ).scalar()
        assert n == 1, "semantic note was not accumulated"

        # === PHASE 2: USE =================================================
        # A NEW change whose signals overlap the accumulated knowledge. The
        # SAME retriever verify/seed use folds it into the verification contract.
        async with sf() as s:
            retriever = _production_retriever(s, settings=settings, workspace_id=workspace_id)
            service = VerificationService(session=s, llm=_NoopJudge(), retriever=retriever)
            contract = await service.assemble_contract(
                declared_contract=None,
                written_paths=["backend/api/search.py"],
                final_text="add request throttling to the search endpoint",
            )

        assert contract is not None, "no contract assembled — knowledge was not folded in"
        knowledge_checks = [
            c for c in contract.checks if c.rationale == RETRIEVED_KNOWLEDGE_RATIONALE
        ]
        assert knowledge_checks, "verify contract carries no retrieved-knowledge check"
        criteria = "\n".join(crit for c in knowledge_checks for crit in c.criteria)
        low = criteria.lower()
        # The accumulated SEMANTIC note is used (found by meaning — query said
        # "throttling", the note said "rate limiter" — and folded as a criterion).
        assert "related note" in low and "search-api" in low, criteria
        # The accumulated PRIOR DECISION is used, with its concrete content.
        assert "prior decision" in low, criteria
        assert "token-bucket" in low, criteria
    finally:
        async with sf() as s:
            await s.execute(
                text("DELETE FROM note_embeddings WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            )
            await s.commit()
        await engine.dispose()
