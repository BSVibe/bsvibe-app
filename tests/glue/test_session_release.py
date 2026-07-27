"""Drive-session-release — the DB connection is released across the executor turn.

The bug (live outage): ``AgentWorker.drive_once`` drove each run inside ONE open
transaction holding a ``SELECT ... FOR UPDATE SKIP LOCKED`` row-lock (and its
pooled DB connection) for the WHOLE drive — including the multi-minute external
executor turn. ~15 concurrent held connections exhausted the pool and every DB
endpoint (``/workers/heartbeat``) hung → full outage.

The fix is three coordinated changes:
  (A) the executor poll is connection-free (short session per read);
  (B) ``_drive_loop`` commits at every turn boundary so no connection is held
      across the ``complete()`` await;
  (C) the held ``FOR UPDATE`` claim is replaced by a committed atomic claim
      (``claimed_at``/``claimed_by``) plus a stale-claim reaper.

These tests prove each property. Real Postgres is required for the pool-exhaustion
proof (a) and the SKIP LOCKED multi-worker safety proof (b) — SQLite reproduces
neither; the rest run on SQLite or PG.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config import get_settings
from backend.data import Base
from backend.dispatch.adapter import ExecutorCapacitySaturated
from backend.extensions.skill.loader import SkillLoader
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.agent_loop import (
    LoopResult,
    LoopToolCall,
    LoopTurn,
    RunOrchestrator,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.workers.agent_worker import (
    AgentExecutionDeps,
    AgentWorker,
    AgentWorkerConfig,
)

from .._support import db_engine, pg_url, use_real_pg

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class _ScriptedLlm:
    """Deterministic ``LoopLlm`` — pops the next pre-programmed turn FIFO."""

    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        if not self._turns:
            raise AssertionError("ScriptedLlm exhausted")
        return self._turns.pop(0)


def _tc(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


def _verified_turns() -> list[LoopTurn]:
    return [
        LoopTurn(
            content="Writing the deliverable and declaring the check.",
            tool_calls=(
                _tc(
                    "declare_verification",
                    checks=[{"kind": "command", "command": "test -f answer.txt"}],
                ),
                _tc("file_write", path="answer.txt", content="42\n"),
            ),
        ),
        LoopTurn(content="Done.", tool_calls=()),
    ]


def _skill_loader_for(root: Path):
    def _inner(ws_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(root / "skills" / str(ws_id))
        loader.load_all()
        return loader

    return _inner


def _deps(root: Path, orchestrator_factory) -> AgentExecutionDeps:
    return AgentExecutionDeps(
        skill_loader_for=_skill_loader_for(root),
        orchestrator_factory=orchestrator_factory,
        workspace_root=root,
    )


async def _seed_workspace(sf: async_sessionmaker, ws_id: uuid.UUID) -> None:
    async with sf() as s:
        s.add(
            WorkspaceRow(
                id=ws_id,
                name="test-ws",
                safe_mode=False,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()


async def _seed_run(
    sf: async_sessionmaker,
    *,
    ws_id: uuid.UUID,
    status: RunStatus = RunStatus.OPEN,
    payload: dict[str, Any] | None = None,
    claimed_at: datetime | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws_id,
                request_id=uuid.uuid4(),
                status=status,
                payload=payload if payload is not None else {"frame": {"skill_match": None}},
                claimed_at=claimed_at,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return run_id


# --------------------------------------------------------------------------
# (a) THE definitive proof — no DB connection held during the executor await.
# --------------------------------------------------------------------------


async def test_no_connection_held_during_executor_await(tmp_path: Path) -> None:
    """Park a drive inside the executor ``complete()`` await against a pool of
    exactly ONE connection; a concurrent independent DB query (standing in for
    ``/workers/heartbeat``) must still succeed. Pre-fix the drive held the pool's
    only connection idle-in-transaction across the turn and this query deadlocked
    until pool-timeout; post-fix the turn-boundary commit (B) + connection-free
    claim (C) release it, so the query returns at once."""
    if not use_real_pg():
        pytest.skip("real Postgres required — SQLite has no QueuePool to exhaust")

    # A pool of exactly ONE connection, no overflow: ANY connection the parked
    # drive holds blocks the concurrent query. pool_timeout keeps the failure
    # fast (a blocked checkout raises within 3s rather than the default 30s).
    engine = create_async_engine(pg_url(), future=True, pool_size=1, max_overflow=0, pool_timeout=3)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)  # checkfirst — no-op on migrated PG

        ws_id = uuid.uuid4()
        await _seed_workspace(sf, ws_id)
        # request_id=None + a pre-seeded "frame" → framing is skipped (no frame LLM needed).
        run_id = await _seed_run(sf, ws_id=ws_id, payload={"frame": {"skill_match": None}})
        # Clear request_id so _frame_and_drive never fetches a Request.
        async with sf() as s:
            await s.execute(
                update(ExecutionRun).where(ExecutionRun.id == run_id).values(request_id=None)
            )
            await s.commit()

        parked = asyncio.Event()
        release = asyncio.Event()

        class _BlockingLlm:
            async def complete(
                self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
            ) -> LoopTurn:
                parked.set()
                await release.wait()
                return LoopTurn(content="done", tool_calls=())

        def _factory(session, run):
            return RunOrchestrator(
                session=session, llm=_BlockingLlm(), sandbox_manager=NoopSandboxManager()
            )

        worker = AgentWorker(session_factory=sf, execution=_deps(tmp_path, _factory))
        drive_task = asyncio.create_task(worker.drive_once())
        try:
            await asyncio.wait_for(parked.wait(), timeout=15)

            # The drive is now parked in the executor await. With a 1-connection
            # pool this MUST succeed only if the drive holds zero connections.
            async def _heartbeat() -> int:
                async with sf() as s:
                    rows = (await s.execute(select(ExecutionRun.id))).all()
                    return len(rows)

            # The proof is that the query RETURNS (a connection was free) rather
            # than deadlocking on the 1-connection pool until pool-timeout. The
            # exact count is not load-bearing — the shared CI Postgres may carry
            # rows from other suites — only that at least our seeded run is seen.
            got = await asyncio.wait_for(_heartbeat(), timeout=5)
            assert got >= 1, "concurrent DB query returned while the drive was parked"
        finally:
            release.set()
            await asyncio.wait_for(drive_task, timeout=30)
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# (b) Multi-worker claim safety — SKIP LOCKED, no double-claim.
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sf():
    # Full Base.metadata: the verified-terminal path (finish_verified) writes a
    # Deliverable + a notification-outbox row, so every imported table must exist.
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def test_two_workers_never_double_claim_one_batch(sf) -> None:
    """Two workers claim the same batch of OPEN runs concurrently. The
    ``UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING``
    claim guarantees every run is claimed by EXACTLY ONE worker — the returned
    id-sets are disjoint and together cover the whole batch."""
    if not use_real_pg():
        pytest.skip("real Postgres required — SKIP LOCKED is a no-op on SQLite")

    ws_id = uuid.uuid4()
    await _seed_workspace(sf, ws_id)
    run_ids = {await _seed_run(sf, ws_id=ws_id) for _ in range(6)}

    w1 = AgentWorker(session_factory=sf, config=AgentWorkerConfig(batch_size=10))
    w2 = AgentWorker(session_factory=sf, config=AgentWorkerConfig(batch_size=10))

    claimed1, claimed2 = await asyncio.gather(
        w1._claim_runs_for_drive(), w2._claim_runs_for_drive()
    )
    set1, set2 = set(claimed1), set(claimed2)

    # None double-claimed.
    assert set1.isdisjoint(set2), f"a run was claimed by BOTH workers: {set1 & set2}"
    # Together they cover the whole batch.
    assert set1 | set2 == run_ids
    # Every claimed run is RUNNING with a claimed_by matching one of the workers.
    async with sf() as s:
        for run_id in run_ids:
            run = await s.get(ExecutionRun, run_id)
            assert run.status is RunStatus.RUNNING
            assert run.claimed_by in {w1._worker_id, w2._worker_id}
            assert run.claimed_at is not None


# --------------------------------------------------------------------------
# (c) Stale-claim reaper + paused-on-decision double guard.
# --------------------------------------------------------------------------


async def test_reaper_resets_stale_claims_but_never_paused_on_decision(sf, monkeypatch) -> None:
    """The reaper resets a stale claim (past lease, no pending Decision) back to
    OPEN, but a run paused on a Decision (claimed_at NULL + a pending Decision)
    is DOUBLE-guarded and never reaped, and a fresh in-flight claim (within the
    lease) is left alone."""
    settings = get_settings()
    monkeypatch.setattr(settings, "executor_task_timeout_s", 1.0, raising=False)  # lease = 2s
    ws_id = uuid.uuid4()
    await _seed_workspace(sf, ws_id)

    stale = datetime.now(tz=UTC) - timedelta(seconds=60)
    fresh = datetime.now(tz=UTC)

    # (1) stale claim, no decision → reaped.
    r_stale = await _seed_run(sf, ws_id=ws_id, status=RunStatus.RUNNING, claimed_at=stale)
    # (2) paused on decision: claimed_at NULL + pending Decision → NOT reaped.
    r_paused = await _seed_run(sf, ws_id=ws_id, status=RunStatus.RUNNING, claimed_at=None)
    # (3) fresh claim, within lease → NOT reaped.
    r_fresh = await _seed_run(sf, ws_id=ws_id, status=RunStatus.RUNNING, claimed_at=fresh)
    # (4) stale claim BUT pending decision → NOT reaped (second guard).
    r_stale_decided = await _seed_run(sf, ws_id=ws_id, status=RunStatus.RUNNING, claimed_at=stale)
    async with sf() as s:
        for rid in (r_paused, r_stale_decided):
            s.add(
                Decision(
                    id=uuid.uuid4(),
                    run_id=rid,
                    workspace_id=ws_id,
                    decision="ask_user_question",
                    status=DecisionStatus.PENDING,
                    payload={},
                )
            )
        await s.commit()

    worker = AgentWorker(session_factory=sf, settings=settings)
    reaped = await worker._reap_stale_claims()
    assert reaped == 1

    async with sf() as s:
        assert (await s.get(ExecutionRun, r_stale)).status is RunStatus.OPEN
        assert (await s.get(ExecutionRun, r_stale)).claimed_at is None
        assert (await s.get(ExecutionRun, r_paused)).status is RunStatus.RUNNING
        assert (await s.get(ExecutionRun, r_fresh)).status is RunStatus.RUNNING
        assert (await s.get(ExecutionRun, r_stale_decided)).status is RunStatus.RUNNING


# --------------------------------------------------------------------------
# (d) Resume — a pre-framed (re-opened) run drives to terminal, framing skipped.
# --------------------------------------------------------------------------


async def test_resume_skips_framing_and_terminates_once(sf, tmp_path: Path) -> None:
    """A run that already carries a ``frame`` (the shape a needs_decision run has
    after the founder resolves it and it is re-opened RUNNING → OPEN) drives to a
    verified terminal WITHOUT re-invoking the frame stage, producing exactly one
    Deliverable."""
    ws_id = uuid.uuid4()
    await _seed_workspace(sf, ws_id)
    run_id = await _seed_run(
        sf,
        ws_id=ws_id,
        payload={"intent_text": "build it", "frame": {"skill_match": None}},
    )

    llm = _ScriptedLlm(_verified_turns())

    def _factory(session, run):
        return RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())

    worker = AgentWorker(session_factory=sf, execution=_deps(tmp_path, _factory))

    framed: list[Any] = []
    orig = worker._frame_stage.frame

    async def _spy_frame(*a, **k):
        framed.append(1)
        return await orig(*a, **k)

    worker._frame_stage.frame = _spy_frame  # type: ignore[method-assign]

    driven = await worker.drive_once()
    assert driven == 1
    assert framed == [], "frame stage was re-invoked on a pre-framed (resumed) run"

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run.status is RunStatus.REVIEW_READY
        assert run.claimed_at is None  # claim cleared on terminal exit
        deliverables = (
            (await s.execute(select(Deliverable).where(Deliverable.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(deliverables) == 1


# --------------------------------------------------------------------------
# (e) Saturation yield-back — act saturation resets RUNNING → OPEN, re-picked.
# --------------------------------------------------------------------------


class _SaturatingCompute:
    """A ``RunCompute`` whose drive raises ``ExecutorCapacitySaturated`` — as the
    act-stage ExecutorAdapter does when all live workers are at capacity."""

    async def run(self, *, run: ExecutionRun, workspace_dir: Path) -> LoopResult:
        raise ExecutorCapacitySaturated("all live workers at capacity")


async def test_act_saturation_resets_run_open_and_is_repicked(sf, tmp_path: Path) -> None:
    """An ``ExecutorCapacitySaturated`` out of the act drive resets the claimed
    RUNNING run back to OPEN with ``claimed_at`` cleared (NOT failed, no decision
    state), so a later ``drive_once`` re-picks it."""
    ws_id = uuid.uuid4()
    await _seed_workspace(sf, ws_id)
    run_id = await _seed_run(sf, ws_id=ws_id)

    def _saturating_factory(session, run):
        return _SaturatingCompute()

    worker = AgentWorker(session_factory=sf, execution=_deps(tmp_path, _saturating_factory))
    driven = await worker.drive_once()
    assert driven == 0  # a yielded run was not driven

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run.status is RunStatus.OPEN
        assert run.claimed_at is None
        assert run.claimed_by is None

    # It is re-pickable: a second drive_once with a healthy orchestrator drives it.
    llm = _ScriptedLlm(_verified_turns())

    def _ok_factory(session, run):
        return RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())

    worker2 = AgentWorker(session_factory=sf, execution=_deps(tmp_path, _ok_factory))
    assert await worker2.drive_once() == 1
    async with sf() as s:
        assert (await s.get(ExecutionRun, run_id)).status is RunStatus.REVIEW_READY


# --------------------------------------------------------------------------
# (f) Connection-free poll — await_completion opens a short session per read.
# --------------------------------------------------------------------------


async def test_await_completion_poll_is_connection_free(monkeypatch) -> None:
    """``await_completion`` given a ``session_factory`` opens a SHORT session per
    DB read and holds NONE across the poll sleep — so no pooled connection is
    held idle across the (up to 30-minute) executor turn. Spy the factory: it is
    opened more than once and is never held open concurrently."""
    from backend.executors import dispatch
    from backend.executors.db import ExecutorTaskRow

    from .._support import shared_file_sessionmaker

    monkeypatch.setattr(dispatch, "_AWAIT_POLL_INTERVAL_S", 0.05)

    class _SpyFactory:
        def __init__(self, inner: async_sessionmaker) -> None:
            self._inner = inner
            self.opens = 0
            self.concurrent = 0
            self.max_concurrent = 0

        def __call__(self):
            spy = self

            class _Ctx:
                async def __aenter__(self_ctx):
                    spy.opens += 1
                    spy.concurrent += 1
                    spy.max_concurrent = max(spy.max_concurrent, spy.concurrent)
                    self_ctx._cm = spy._inner()
                    return await self_ctx._cm.__aenter__()

                async def __aexit__(self_ctx, *exc):
                    spy.concurrent -= 1
                    return await self_ctx._cm.__aexit__(*exc)

            return _Ctx()

    class _FakePubSub:
        async def subscribe(self, chan: str) -> None: ...
        async def get_message(
            self,
            *,
            ignore_subscribe_messages: bool,
            timeout: float,  # noqa: ASYNC109 — mirrors redis pubsub's own signature
        ):
            await asyncio.sleep(timeout)
            return None

        async def unsubscribe(self, chan: str) -> None: ...
        async def aclose(self) -> None: ...

    class _FakeRedis:
        def pubsub(self):
            return _FakePubSub()

        async def publish(self, *a, **k) -> None: ...

    async with shared_file_sessionmaker() as inner:
        task_id = uuid.uuid4()
        async with inner() as s:
            s.add(
                ExecutorTaskRow(
                    id=task_id,
                    workspace_id=uuid.uuid4(),
                    executor_type="claude_code",
                    prompt="hi",
                    system="",
                    workspace_dir=".",
                    status="dispatched",
                )
            )
            await s.commit()

        async def _flip_terminal_after_delay() -> None:
            await asyncio.sleep(0.18)
            async with inner() as s:
                await s.execute(
                    update(ExecutorTaskRow)
                    .where(ExecutorTaskRow.id == task_id)
                    .values(status="done", output="ok")
                )
                await s.commit()

        spy = _SpyFactory(inner)
        flip = asyncio.create_task(_flip_terminal_after_delay())
        async with inner() as bound:
            completed = await dispatch.await_completion(
                _FakeRedis(),
                session=bound,
                task_id=task_id,
                timeout_s=5,
                session_factory=spy,  # type: ignore[arg-type]
            )
        await flip
        assert completed.status == "done"
        assert completed.output == "ok"
        # A short session per read — opened repeatedly, never held across a poll.
        assert spy.opens >= 2, f"expected multiple short-session reads, got {spy.opens}"
        assert spy.max_concurrent == 1, "a session was held open across a poll sleep"
