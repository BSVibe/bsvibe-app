"""Decision resolve end-to-end — pause → resolve → resume → review_ready.

Proves the founder can resolve a paused-run Decision and resume the run
(Workflow §5 #4 / §12.5 #8):

    AgentWorker.claim_once   → ExecutionRun (OPEN) + Request RUNNING
    AgentWorker.drive_once   → RunOrchestrator.run
        work LLM calls ask_user_question
        → Decision (pending) + run stays RUNNING (paused)
    POST /api/v1/checkpoints/{id}/resolve {answer}
        → Decision resolved + resolution stored
        + run re-OPENed + resolved_decisions in payload
    AgentWorker.drive_once   → RunOrchestrator.run (now completes)
        → ExecutionRun REVIEW_READY (the loop saw the founder's answer)

The work LLM is a deterministic scripted stub swapped between drives via a
mutable holder; the sandbox is the host-side NoopSandboxManager (no Docker, no
real model). Runs on in-memory SQLite by default, real Postgres when
BSVIBE_DATABASE_URL is set (mirrors the other glue tests).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.connectors.db import ConnectorAccountRow
from backend.extensions.skill.loader import SkillLoader
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.application.checkpoint_resolution import resolve_checkpoint
from backend.workflow.application.delivery.connector_dispatch import (
    build_github_workspace_provisioner,
)
from backend.workflow.infrastructure.db import Decision, DecisionStatus, ExecutionRun, RunStatus
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.workers.agent_worker import AgentExecutionDeps, AgentWorker

from .._support import BuildFrameLlm, db_engine, fake_current_user

_GH_KEY = b"0123456789abcdef0123456789abcdef"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class _ScriptedLlm:
    """A deterministic ``LoopLlm`` — pops the next pre-programmed turn FIFO."""

    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)
        self.seen_messages: list[list[dict]] = []

    async def complete(self, *, messages, tools):  # type: ignore[no-untyped-def]
        self.seen_messages.append(list(messages))
        if not self._turns:
            raise AssertionError("ScriptedLlm exhausted — loop requested an unscripted turn")
        return self._turns.pop(0)


class _LlmHolder:
    """Mutable seam — the orchestrator factory delegates to whichever LLM is
    set, so the test can swap the scripted script between successive drives."""

    def __init__(self) -> None:
        self.current: _ScriptedLlm | None = None

    async def complete(self, *, messages, tools):  # type: ignore[no-untyped-def]
        assert self.current is not None, "no scripted LLM set for this drive"
        return await self.current.complete(messages=messages, tools=tools)


def _tc(name: str, **arguments) -> LoopToolCall:  # type: ignore[no-untyped-def]
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


def _ask_script() -> _ScriptedLlm:
    """First drive: the work LLM blocks on a founder question."""
    return _ScriptedLlm(
        [
            LoopTurn(
                content="I cannot proceed without a decision.",
                tool_calls=(
                    _tc(
                        "ask_user_question",
                        question="Which database should I target?",
                    ),
                ),
            ),
        ]
    )


def _complete_script() -> _ScriptedLlm:
    """Second drive (after resolution): the work LLM now completes."""
    return _ScriptedLlm(
        [
            LoopTurn(
                content="Using the chosen database; writing the deliverable.",
                tool_calls=(
                    _tc(
                        "declare_verification",
                        checks=[{"kind": "command", "command": "test -f answer.txt"}],
                    ),
                    _tc("file_write", path="answer.txt", content="postgres\n"),
                ),
            ),
            LoopTurn(content="Done — answer.txt written.", tool_calls=()),
        ]
    )


def _execution_deps(
    sf_: async_sessionmaker[AsyncSession], workspace_root: Path, holder: _LlmHolder
) -> AgentExecutionDeps:
    def _skill_loader_for(ws_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(workspace_root / "skills" / str(ws_id))
        loader.load_all()
        return loader

    return AgentExecutionDeps(
        skill_loader_for=_skill_loader_for,
        orchestrator_factory=lambda session, _run: RunOrchestrator(
            session=session, llm=holder, sandbox_manager=NoopSandboxManager()
        ),
        workspace_root=workspace_root,
        # prod always routes a frame model; the stage refuses to guess without one
        frame_llm=BuildFrameLlm(),
    )


async def _seed_open_request(
    sf_: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID
) -> uuid.UUID:
    """Seed an OPEN Request so AgentWorker.claim_once mints an ExecutionRun."""
    async with sf_() as s:
        trigger = TriggerEventRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            source="direct",
            trigger_kind=TriggerKind.DIRECT,
            idempotency_key=uuid.uuid4().hex,
            payload={"text": "build the answer file"},
        )
        s.add(trigger)
        await s.flush()
        req = RequestRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            trigger_event_id=trigger.id,
            status=RequestStatus.OPEN,
            payload={"text": "build the answer file"},
        )
        s.add(req)
        await s.commit()
        return req.id


@pytest_asyncio.fixture
async def client(sf, founder_id: uuid.UUID, workspace_id: uuid.UUID):
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


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


# --------------------------------------------------------------------------
# The pause → resolve → resume → review_ready path
# --------------------------------------------------------------------------


async def test_pause_resolve_resume_to_review_ready(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    founder_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    holder = _LlmHolder()
    deps = _execution_deps(sf, tmp_path, holder)
    agent = AgentWorker(session_factory=sf, execution=deps)

    # 1. Seed + claim a Request → ExecutionRun (OPEN).
    await _seed_open_request(sf, workspace_id)
    assert await agent.claim_once() == 1

    # 2. First drive: the work LLM blocks → Decision (pending), run RUNNING.
    holder.current = _ask_script()
    assert await agent.drive_once() == 1

    async with sf() as s:
        run = (await s.execute(select(ExecutionRun))).scalar_one()
        run_id = run.id
        assert run.status is RunStatus.RUNNING  # paused, not terminal

        decision = (await s.execute(select(Decision))).scalar_one()
        assert decision.status is DecisionStatus.PENDING
        assert decision.run_id == run_id
        assert "database" in (decision.payload.get("question") or "")
        decision_id = decision.id

    # 3. List checkpoints → only the pending one.
    resp = await client.get("/api/v1/checkpoints")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    assert items[0]["id"] == str(decision_id)
    assert "database" in items[0]["question"]

    # 4. Resolve the checkpoint → decision resolved + run re-OPENed.
    resolve = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"answer": "Use Postgres"},
    )
    assert resolve.status_code == 200, resolve.text
    body = resolve.json()
    assert body["status"] == "resolved"
    assert body["resolution"] == "Use Postgres"
    assert body["run_status"] == "open"

    async with sf() as s:
        decision = await s.get(Decision, decision_id)
        assert decision is not None
        assert decision.status is DecisionStatus.RESOLVED
        assert decision.resolution == "Use Postgres"
        assert decision.resolved_at is not None
        assert decision.resolved_by == founder_id

        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.OPEN  # resumed
        resolved = run.payload.get("resolved_decisions")
        assert isinstance(resolved, list) and len(resolved) == 1
        assert resolved[0]["answer"] == "Use Postgres"
        assert resolved[0]["decision_id"] == str(decision_id)

    # 5. Resolving the (now non-pending) checkpoint again → 404.
    again = await client.post(f"/api/v1/checkpoints/{decision_id}/resolve", json={"answer": "no"})
    assert again.status_code == 404

    # 6. List checkpoints → now empty (the only one is resolved).
    resp = await client.get("/api/v1/checkpoints")
    assert resp.status_code == 200
    assert resp.json() == []

    # 7. Second drive: the loop now completes → REVIEW_READY (saw the answer).
    holder.current = _complete_script()
    assert await agent.drive_once() == 1

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY

    # The loop's initial messages carried the founder's resolution as context.
    seeded = "".join(
        msg.get("content") or "" for batch in holder.current.seen_messages for msg in batch
    )
    assert "Use Postgres" in seeded
    # The artifact actually landed in the run's workspace.
    assert (tmp_path / str(run_id) / "answer.txt").read_text() == "postgres\n"


# --------------------------------------------------------------------------
# Regression: the resume path exercised through the GITHUB provisioner.
#
# The direct-path resume test above injects NO workspace_provisioner, so it
# never re-runs the one-time github clone on the SECOND drive — which is exactly
# the gap that let the prod re-clone stall ship. This test wires the real github
# provisioner against a LOCAL bare repo: the first drive clones the checkout,
# the run pauses on a Decision, the founder resolves it, and the SECOND drive
# must REUSE the existing checkout (not re-clone, which git refuses on a
# non-empty dir → GitError → tick rollback → run stalled OPEN forever).
# --------------------------------------------------------------------------


async def _git(*args: str, cwd: Path | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return out.decode().strip()


async def _make_bare_remote(tmp_path: Path) -> Path:
    """A bare repo seeded with an initial commit on ``main`` — stands in for
    github.com so the provisioner's clone is real but offline."""
    bare = tmp_path / "remote.git"
    await _git("init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    await _git("clone", str(bare), str(seed))
    await _git("config", "user.email", "t@bsvibe.dev", cwd=seed)
    await _git("config", "user.name", "Test", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    await _git("add", "-A", cwd=seed)
    await _git("commit", "-m", "initial", cwd=seed)
    await _git("push", "origin", "main", cwd=seed)
    return bare


async def _seed_github_connector(
    session: AsyncSession, cipher: CredentialCipher, workspace_id: uuid.UUID
) -> None:
    session.add(
        ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            connector="github",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("ghp_test_token"),
            delivery_config={"repo": "owner/name", "base_branch": "main"},
            is_active=True,
        )
    )
    await session.commit()


def _github_execution_deps(
    workspace_root: Path, cipher: CredentialCipher, bare: Path, holder: _LlmHolder
) -> AgentExecutionDeps:
    def _skill_loader_for(ws_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(workspace_root / "skills" / str(ws_id))
        loader.load_all()
        return loader

    provisioner = build_github_workspace_provisioner(
        cipher=cipher, remote_url_for=lambda _repo: bare.as_uri()
    )
    return AgentExecutionDeps(
        skill_loader_for=_skill_loader_for,
        orchestrator_factory=lambda session, _run: RunOrchestrator(
            session=session, llm=holder, sandbox_manager=NoopSandboxManager()
        ),
        workspace_root=workspace_root,
        workspace_provisioner=provisioner,
        frame_llm=BuildFrameLlm(),
    )


async def test_pause_resolve_resume_github_run_reuses_checkout(
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    founder_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    """Producer-existence proof: a github-bound run framed + provisioned on the
    first drive, paused on a Decision, then resolved, must RESUME without the
    provisioner re-cloning the existing checkout. Before the fix the second
    ``drive_once`` raises ``GitError`` (re-clone of a non-empty dir) and the run
    stalls OPEN; after the fix it reuses the checkout and reaches REVIEW_READY —
    i.e. ``runner.drive`` fires (the continuation producer exists)."""
    cipher = CredentialCipher(_GH_KEY)
    bare = await _make_bare_remote(tmp_path)
    workspace_root = tmp_path / "runs"

    async with sf() as s:
        await _seed_github_connector(s, cipher, workspace_id)

    holder = _LlmHolder()
    deps = _github_execution_deps(workspace_root, cipher, bare, holder)
    agent = AgentWorker(session_factory=sf, execution=deps)

    # 1. Seed + claim a Request → ExecutionRun (OPEN).
    await _seed_open_request(sf, workspace_id)
    assert await agent.claim_once() == 1

    # 2. First drive: provisioner clones the bare repo → the checkout exists,
    #    then the work LLM blocks on a founder question → run RUNNING (paused).
    holder.current = _ask_script()
    assert await agent.drive_once() == 1

    async with sf() as s:
        run = (await s.execute(select(ExecutionRun))).scalar_one()
        run_id = run.id
        assert run.status is RunStatus.RUNNING
        decision = (await s.execute(select(Decision))).scalar_one()
        decision_id = decision.id

    # The provisioner cloned a real checkout into the run workspace.
    checkout = workspace_root / str(run_id)
    assert (checkout / ".git").exists()

    # 3. Resolve the Decision → run RUNNING → OPEN (re-picked by drive_once).
    async with sf() as s:
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="Use Postgres",
            actor_id=founder_id,
        )
        await s.commit()

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.OPEN

    # 4. Second drive: the provisioner re-enters with the SAME (non-empty)
    #    checkout. It MUST reuse it (no re-clone → no GitError → no stall); the
    #    loop then completes → REVIEW_READY.
    holder.current = _complete_script()
    assert await agent.drive_once() == 1

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY
    # The checkout was preserved across the resume (not wiped by a fresh clone).
    assert (checkout / ".git").exists()


async def test_resolve_cross_workspace_checkpoint_404(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
) -> None:
    """A pending Decision in a different workspace is not resolvable → 404."""
    other_ws = uuid.uuid4()
    async with sf() as s:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=other_ws,
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
            workspace_id=other_ws,
            decision="ask_user_question",
            payload={"question": "secret?"},
            status=DecisionStatus.PENDING,
        )
        s.add(decision)
        await s.commit()
        decision_id = decision.id

    resp = await client.post(f"/api/v1/checkpoints/{decision_id}/resolve", json={"answer": "x"})
    assert resp.status_code == 404

    # And the cross-workspace pending decision never shows in the caller's list.
    listing = await client.get("/api/v1/checkpoints")
    assert listing.status_code == 200
    assert listing.json() == []


async def test_resolve_unknown_checkpoint_404(client: httpx.AsyncClient) -> None:
    resp = await client.post(f"/api/v1/checkpoints/{uuid.uuid4()}/resolve", json={"answer": "x"})
    assert resp.status_code == 404


async def test_resolve_rejects_empty_answer(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
) -> None:
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
            payload={"question": "q?"},
            status=DecisionStatus.PENDING,
        )
        s.add(decision)
        await s.commit()
        decision_id = decision.id

    # L-D2: an empty answer with no action_key is now a 400 from the handler
    # (was a 422 from pydantic min_length when ``answer`` was strictly required;
    # relaxed to ``answer: str = ""`` to allow action-only POSTs, which then
    # carries the non-empty check into the resolve_checkpoint body).
    resp = await client.post(f"/api/v1/checkpoints/{decision_id}/resolve", json={"answer": ""})
    assert resp.status_code == 400
