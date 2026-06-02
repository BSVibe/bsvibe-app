"""D5 — the trust ratchet closes ACROSS runs (the moat test).

BSVibe's un-copyable core is a trust ratchet: a founder decision is supervised
ONCE, enters BSage (the vault knowledge ontology), and FUTURE runs retrieve it
so the same question is not re-asked. The audit flagged that resolved Decisions
might be consumed *within-run only* (folded into the paused run's resumption
messages) and never deposited for *cross-run* reuse — if so, the moat is hollow.

The pieces all exist and are individually tested:

* ``tests/api/test_checkpoints_settle.py`` — the PRODUCER write path: resolving a
  checkpoint emits a ``decision_resolution`` settle activity, and the
  ``SettleWorker`` drains it into a ``garden/seedling`` note (real path, no
  pre-seed).
* ``tests/test_knowledge_utilization_e2e.py`` — the CONSUMER read path: a prior
  decision in the vault is folded into a verify contract. BUT that test
  *pre-seeds the vault* with ``_seed_prior_decision`` (bypassing the producer)
  and never asserts the NEGATIVE half — so it is close to a tautology and would
  NOT catch a broken producer→consumer wiring.

This module closes the gap with a single CROSS-RUN test that chains the REAL
producer (resolve endpoint + SettleWorker, no pre-seeded vault) into the
production retriever (``KnowledgeFactory.retriever()`` — the same composite
``backend.workflow.infrastructure.workers.run._retriever_for`` injects into the orchestrator's
work/verify context), and asserts BOTH halves of the ratchet:

* POSITIVE — with Run A's resolution deposited via the real path, Run B's
  retriever surfaces the prior decision for an overlapping signal.
* NEGATIVE — without Run A's resolution (a fresh workspace vault), the SAME
  retriever + SAME signal surfaces NOTHING. This is what makes it a real
  ratchet test and not a tautology: a no-op (decision never deposited) fails.

Anti-``absence-measurement-validity-check`` / ``mock-fixtures-hide-wiring-bugs``:
the producer is proven to have actually run before the consumer is measured —
the test asserts the garden note physically exists in the vault on disk (written
by the real SettleWorker), so "Run B retrieves nothing" can only mean a broken
retrieval link, never a writer that silently never fired.

Runs fully in CI on in-memory SQLite + a tmp_path vault: the embedder is
disabled by default (``knowledge_embedding_model == ""``), so the production
retriever is the filesystem-only base composite (canon + resolved-decisions +
negative) — no Postgres, no Ollama required for the decision-resolution seam.
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
from backend.knowledge.factory import KnowledgeFactory
from backend.knowledge.infrastructure.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)

from ._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

_REGION = "us-1"

# The blocking question Run A pauses on and the founder's answer. The signal
# Run B carries shares salient tokens ("rate limit"/"search"/"endpoint") with
# the question + answer so the ResolvedDecisionsRetriever's overlap filter
# matches — exactly how a future *related* run reaches the prior decision.
_QUESTION = "Should the public search endpoint enforce a rate limit?"
_ANSWER = "Yes — token-bucket, 10 requests per second per API key"
_INTENT = "harden the public search endpoint"

# Run B's related signal (a NEW change touching the same area). Overlaps the
# decision tokens (search / endpoint / rate / limit) but is its own request.
_RUN_B_SIGNAL = "add request throttling and a rate limit to the search endpoint"

# An unrelated signal that must NOT pull the decision in even when it IS on
# record — proves the retrieval is signal-filtered, not a blanket dump. Shares
# zero salient (>= 3-char) tokens with the decision's question / answer /
# intent, so the overlap filter correctly pulls nothing.
_UNRELATED_SIGNAL = "redesign onboarding email copy"


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(sf, workspace_id: uuid.UUID, founder_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_run_a_pending(
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Run A: a paused run + the pending Decision it stopped on.

    Mirrors the orchestrator's ``ask_user_question`` terminal — a RUNNING run
    with a PENDING ``ask_user_question`` Decision carrying the blocking question.
    Returns ``(run_id, decision_id)``.
    """
    async with sf() as s:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload={"text": _INTENT, "intent_text": _INTENT},
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
            payload={"question": _QUESTION},
            status=DecisionStatus.PENDING,
        )
        s.add(decision)
        await s.commit()
        return run.id, decision.id


def _production_retriever(vault_root: Path, workspace_id: uuid.UUID):
    """The retriever Run B's work/verify context actually uses.

    Reproduces the filesystem branch of ``backend.workflow.infrastructure.workers.run._retriever_for``:
    with the embedder disabled (default), it is exactly
    ``KnowledgeFactory.retriever()`` — the composite of CanonConceptRetriever +
    ResolvedDecisionsRetriever + NegativePatternRetriever rooted at the same
    ``<vault_root>/<region>/<workspace_id>/`` boundary the SettleWorker writes
    into. No PG / no Ollama needed for the decision-resolution seam.
    """
    return KnowledgeFactory(
        region=_REGION,
        workspace_id=str(workspace_id),
        vault_root=vault_root,
    ).retriever()


async def test_resolved_decision_ratchets_across_runs_e2e(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    """The full cross-run ratchet through the REAL write+read path, both halves.

    1. NEGATIVE precondition — before Run A is resolved, Run B's retriever (the
       production composite over the EMPTY vault) surfaces NOTHING for the
       related signal. This is the baseline the ratchet must beat.
    2. PRODUCER — Run A's founder resolves the Decision via the real resolve
       endpoint; the real SettleWorker drains the emitted settle activity into
       the vault. We PROVE the producer ran by asserting the garden note exists
       on disk (anti-absence-measurement).
    3. POSITIVE — Run B's retriever (SAME composite, SAME vault) now surfaces the
       prior decision WITH its concrete answer for the related signal.
    4. SIGNAL FILTER — an unrelated signal still surfaces nothing, so the ratchet
       is relevance-gated, not a blanket dump.
    """
    vault_root = tmp_path / "vault"

    # === 0. NEGATIVE HALF — empty vault, no decision on record ==============
    retriever_before = _production_retriever(vault_root, workspace_id)
    surfaced_before = await retriever_before.retrieve_for_signals(_RUN_B_SIGNAL)
    assert not any("token-bucket" in s for s in surfaced_before), (
        "ratchet is hollow: Run B surfaced the decision answer with NO prior "
        f"resolution on record — {surfaced_before}"
    )

    # === 1. RUN A — pause on a Decision, founder resolves it (real endpoint) =
    _run_a_id, decision_id = await _seed_run_a_pending(sf, workspace_id)
    resolve = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"answer": _ANSWER},
    )
    assert resolve.status_code == 200, resolve.text

    # === 2. PRODUCER — drain the emitted settle activity into the vault ======
    # Real SettleWorker over the SAME vault root the retriever reads. No
    # pre-seeding: the note can only appear if the producer wiring actually
    # fired (resolve endpoint emitted the settle row → worker absorbed it).
    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=vault_root),
        config=SettleWorkerConfig(default_region=_REGION),
    )
    assert await worker.drain_once() == 1, "settle activity did not drain"

    # PROVE the producer ran: the decision-resolution garden note is physically
    # on disk. If this fails, the producer never fired and any "Run B retrieved
    # nothing" below would be a false negative (absence-measurement trap).
    seedling = vault_root / _REGION / str(workspace_id) / "garden" / "seedling"
    notes = list(seedling.rglob("*.md"))
    bodies = "\n".join(p.read_text(encoding="utf-8") for p in notes)
    assert notes, f"producer never wrote a garden note; vault: {vault_root}"
    assert "token-bucket" in bodies, (
        "garden note exists but does not carry the resolved answer — producer "
        "wiring dropped the decision content"
    )

    # === 3. POSITIVE HALF — Run B retrieves the deposited decision ===========
    retriever_after = _production_retriever(vault_root, workspace_id)
    surfaced_after = await retriever_after.retrieve_for_signals(_RUN_B_SIGNAL)
    joined = "\n".join(surfaced_after)
    assert surfaced_after, "Run B retrieved nothing despite the decision on record"
    assert "prior decision" in joined.lower(), joined
    assert "token-bucket" in joined, (
        "Run B's context omits the prior decision's concrete answer — the "
        f"cross-run ratchet link is broken: {joined}"
    )

    # === 4. SIGNAL FILTER — unrelated signal pulls nothing ==================
    surfaced_unrelated = await retriever_after.retrieve_for_signals(_UNRELATED_SIGNAL)
    assert not any("token-bucket" in s for s in surfaced_unrelated), (
        "decision surfaced on an UNRELATED signal — retrieval is not "
        f"relevance-gated: {surfaced_unrelated}"
    )

    # === 5. RETRACTED HALF — Lift M3a — tombstoned decision stops surfacing ==
    # The fifth half of the ratchet (per ontology-inspect/correct design §4.2):
    # once a prior decision is retracted via the tombstone path (frontmatter
    # ``retracted_at`` set, file NOT deleted), the production retriever's
    # skip-retracted predicate must stop surfacing it for the SAME signal that
    # surfaced it in step 3. The garden note must still exist on disk
    # (provenance preserved); only the retriever's filter changes.
    from datetime import UTC  # noqa: PLC0415
    from datetime import datetime as _dt

    import yaml as _yaml  # noqa: PLC0415

    note_path = notes[0]
    text = note_path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), text
    closing = text.index("\n---\n", 4)
    fm = _yaml.safe_load(text[4:closing]) or {}
    fm["retracted_at"] = _dt.now(tz=UTC).isoformat()
    fm["retracted_by"] = "founder"
    new_text = (
        f"---\n{_yaml.dump(fm, default_flow_style=False).strip()}\n---\n" + text[closing + 5 :]
    )
    note_path.write_text(new_text, encoding="utf-8")

    assert note_path.exists(), "tombstone must NOT delete the note from disk — provenance lost"

    retriever_retracted = _production_retriever(vault_root, workspace_id)
    surfaced_retracted = await retriever_retracted.retrieve_for_signals(_RUN_B_SIGNAL)
    assert not any("token-bucket" in s for s in surfaced_retracted), (
        "retracted decision still surfacing — D5 retriever did not skip "
        f"frontmatter ``retracted_at``: {surfaced_retracted}"
    )
