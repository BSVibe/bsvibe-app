"""G4 — pending checkpoints surface similar prior resolved decisions.

Proposal §5.5: "비슷한 결정을 한 적이 있으면 이전 결정 참고 제안." When the founder
opens a pending Decision, the response carries the relevant prior decisions they
already resolved (matched by question/answer/intent token overlap via the same
:class:`ResolvedDecisionsRetriever` the verify/seed seam uses), so they can
answer consistently instead of re-deciding from scratch.

Read-time surfacing (mirrors the G2 references pattern): the list endpoint runs
the workspace's resolved-decisions retriever per pending Decision and returns
``prior_decisions`` — empty when nothing overlaps, never fabricated.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.api.v1.checkpoints import build_decisions_retriever
from backend.execution.db import Decision, DecisionStatus, ExecutionRun, RunStatus
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenNote
from backend.knowledge.graph.writer_core import GardenWriter
from backend.knowledge.retrieval.resolved_decisions_retriever import ResolvedDecisionsRetriever

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

_REGION = "us-1"


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest_asyncio.fixture
async def client(sf, workspace_id: uuid.UUID, vault_root: Path):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=uuid.uuid4())

    async def _session():
        async with sf() as s:
            yield s

    def _retriever(ws: uuid.UUID = workspace_id) -> ResolvedDecisionsRetriever:
        root = vault_root / _REGION / str(ws)
        root.mkdir(parents=True, exist_ok=True)
        return ResolvedDecisionsRetriever(FileSystemStorage(root))

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[build_decisions_retriever] = _retriever

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_pending(
    sf: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID, *, question: str
) -> uuid.UUID:
    async with sf() as s:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload={},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.flush()
        decision = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="ask_user_question",
            payload={"question": question},
            status=DecisionStatus.PENDING,
        )
        s.add(decision)
        await s.commit()
        return decision.id


async def _seed_resolved_decision_note(
    vault_root: Path, workspace_id: uuid.UUID, *, question: str, answer: str
) -> None:
    root = vault_root / _REGION / str(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    writer = GardenWriter(vault=Vault(root))
    summary = f"Decision resolved — Q: {question} A: {answer}"
    await writer.write_garden(
        GardenNote(
            title=f"Settle: {summary[:80]}",
            content=summary,
            source="settle_worker",
            knowledge_layer="episodic",
            tags=["settle", "verified-run", "decision-resolution"],
            extra_fields={"kind": "decision_resolution", "question": question, "answer": answer},
        )
    )


async def test_pending_checkpoint_surfaces_matching_prior_decision(
    client: httpx.AsyncClient, sf, workspace_id: uuid.UUID, vault_root: Path
) -> None:
    await _seed_resolved_decision_note(
        vault_root,
        workspace_id,
        question="Which database should I target?",
        answer="Use Postgres",
    )
    await _seed_pending(sf, workspace_id, question="Should the new service use which database?")

    r = await client.get("/api/v1/checkpoints")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    prior = rows[0]["prior_decisions"]
    assert any("Postgres" in p for p in prior), prior


async def test_pending_checkpoint_no_match_yields_empty(
    client: httpx.AsyncClient, sf, workspace_id: uuid.UUID, vault_root: Path
) -> None:
    await _seed_resolved_decision_note(
        vault_root, workspace_id, question="Which database?", answer="Use Postgres"
    )
    await _seed_pending(sf, workspace_id, question="rotate the nginx access logs")

    r = await client.get("/api/v1/checkpoints")
    assert r.status_code == 200, r.text
    assert r.json()[0]["prior_decisions"] == []


async def test_pending_checkpoint_empty_vault_yields_empty(
    client: httpx.AsyncClient, sf, workspace_id: uuid.UUID
) -> None:
    await _seed_pending(sf, workspace_id, question="Which database should I use?")

    r = await client.get("/api/v1/checkpoints")
    assert r.status_code == 200, r.text
    assert r.json()[0]["prior_decisions"] == []
