"""Focused unit tests for :class:`ExecutorOrchestrator` (Lift 5b + B2b).

These exercise the orchestrator directly (no AgentRunner, no _factory) against
an in-memory SQLite session + a ``fakeredis`` double — the timeout path and the
malformed-pinned-worker-id parse that the glue e2e doesn't reach.

B2b (executor verification convergence): on worker success the orchestrator now
runs the SAME verification the native loop runs (assemble contract → verify in a
sandbox) and NEVER sets ``ProofState.PROVED`` without a passing
:class:`VerificationResult`. These tests are the anti-regression for the old
fake-PROVED sin: success-but-no-contract → human-review Decision (NOT verified);
contract+PASS → PROVED; contract+FAIL → ``verification_failed`` Decision.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

# Register the executor tables on the shared Base.metadata for create_all.
import backend.executors.db  # noqa: F401
from backend.accounts.models import ModelAccount
from backend.config import Settings
from backend.execution.db import (
    Decision,
    Deliverable,
    ExecutionRun,
    ProofState,
    RunAttempt,
    RunAttemptPhase,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.orchestrator import LoopTurn
from backend.executors import dispatch
from backend.executors import orchestrator as orch
from backend.executors.db import ExecutorTaskRow, WorkerRow
from backend.executors.orchestrator import ExecutorOrchestrator, _parse_uuid
from backend.supervisor.sandbox.protocol import SandboxResult

from .._support import memory_session

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Test doubles (B2b) — a scripted sandbox manager / session, a judge LLM stub,
# and a canon retriever stub. Mirror the VerificationService unit doubles so the
# orchestrator verifies through the SAME machinery the native loop uses.
# --------------------------------------------------------------------------


class FakeBox:
    """A scripted :class:`SandboxSession`. ``exec`` returns a result per command
    from ``exec_map`` (default exit 0); ``read_file`` returns scripted bytes."""

    def __init__(
        self,
        *,
        exec_map: dict[str, SandboxResult] | None = None,
        files: dict[str, bytes] | None = None,
    ) -> None:
        self._exec_map = exec_map or {}
        self._files = files or {}
        self.exec_calls: list[str] = []
        self.read_calls: list[str] = []

    @property
    def workspace_mount(self) -> str:
        return "/work"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.exec_calls.append(command)
        return self._exec_map.get(
            command, SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False)
        )

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        self.read_calls.append(rel_path)
        return self._files.get(rel_path, b"")

    async def write_file(self, rel_path: str, content: bytes) -> None:  # pragma: no cover
        self._files[rel_path] = content

    async def list_dir(self, rel_path: str) -> list[str]:  # pragma: no cover
        return list(self._files)


class FakeSandboxManager:
    """A scripted :class:`SandboxManager` recording acquire/release for the test
    to assert the sandbox is acquired AND released (even on Decision branches)."""

    def __init__(self, box: FakeBox) -> None:
        self._box = box
        self.acquired: list[tuple[uuid.UUID, str]] = []
        self.released: list[uuid.UUID] = []

    async def acquire(self, project_id: uuid.UUID, workspace_path: str) -> FakeBox:
        self.acquired.append((project_id, workspace_path))
        return self._box

    async def release(self, project_id: uuid.UUID) -> None:
        self.released.append(project_id)

    async def reap_idle(self) -> None:  # pragma: no cover
        return None

    async def health(self) -> bool:  # pragma: no cover
        return True


class StubLlm:
    """A deterministic judge LLM — pops the next scripted turn (FIFO)."""

    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._turns:  # pragma: no cover - guards an unscripted judge call
            raise AssertionError("StubLlm exhausted — orchestrator requested an unscripted turn")
        return self._turns.pop(0)


class StubRetriever:
    """A canon retriever returning scripted command/judge criteria as patterns."""

    def __init__(self, patterns: list[str]) -> None:
        self._patterns = patterns
        self.queried: list[str] = []

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        self.queried.append(signals)
        return list(self._patterns)


async def _drive_with_worker_done(
    oc: ExecutorOrchestrator,
    *,
    redis: Any,
    run: ExecutionRun,
    worker_id: uuid.UUID,
    workspace_dir: Path,
    sf: Any,
    output: str = "implemented",
    files: list[dict[str, Any]] | None = None,
    run_workspace_root: str | None = None,
) -> Any:
    """Drive ``oc.run`` while a simulated worker reports a ``done`` result.

    Mirrors the glue e2e contract: the worker learns of the task from the stream
    XADD and reports its result on a SEPARATE session via
    :func:`dispatch.record_result` (which publishes the done channel)."""

    async def _await_task_id() -> uuid.UUID:
        stream = dispatch.worker_stream(worker_id)
        last_id = "0"
        for _ in range(500):
            entries = await redis.xread({stream: last_id}, count=1, block=20)
            if not entries:
                continue
            _name, messages = entries[0]
            for msg_id, fields in messages:
                last_id = msg_id
                return uuid.UUID(fields["task_id"])
        raise AssertionError(f"no task dispatched onto {stream}")

    async def _simulate_worker() -> None:
        task_id = await _await_task_id()
        async with sf() as worker_s:
            await dispatch.record_result(
                worker_s,
                redis,
                task_id=task_id,
                success=True,
                output=output,
                error_message=None,
                files=files,
                run_workspace_root=run_workspace_root,
            )
            await worker_s.commit()

    drive_task = asyncio.create_task(oc.run(run=run, workspace_dir=workspace_dir))
    worker_task = asyncio.create_task(_simulate_worker())
    result = await drive_task
    await worker_task
    return result


async def _make_redis() -> Any:
    try:
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:  # pragma: no cover - declared dep
        pytest.skip("fakeredis not installed")
    client = fakeredis_aio.FakeRedis(decode_responses=True)
    await client.flushdb()
    return client


async def _seed(s: Any, *, executor_type: str = "claude_code") -> tuple[ExecutionRun, ModelAccount]:
    workspace_id = uuid.uuid4()
    worker = WorkerRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name="w",
        labels=[],
        capabilities=[executor_type],
        status="online",
        last_heartbeat=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        token_hash="0" * 64,
        is_active=True,
    )
    s.add(worker)
    account = ModelAccount(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        account_id=uuid.uuid4(),
        provider="executor",
        label="w",
        litellm_model=f"executor/{executor_type}",
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="unknown",
        is_active=True,
        extra_params={"worker_id": str(worker.id), "executor_type": executor_type},
    )
    s.add(account)
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={"intent_text": "do work"},
    )
    s.add(run)
    await s.flush()
    return run, account


def _shared_sqlite_sessionmaker() -> Any:
    """A ``StaticPool`` in-memory SQLite sessionmaker so the orchestrator session
    and the simulated worker's separate session see the SAME database (the B2b
    verify path drives the worker concurrently, like the glue e2e)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import StaticPool  # noqa: PLC0415

    # Register the delivery tables on Base.metadata: write_verified_deliverable
    # writes a DeliveryEventRow, so its table must be materialised by create_all.
    import backend.delivery.db  # noqa: F401, PLC0415
    from backend.data import Base  # noqa: PLC0415

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False), Base


async def test_parse_uuid_variants() -> None:
    u = uuid.uuid4()
    assert _parse_uuid(u) == u
    assert _parse_uuid(str(u)) == u
    assert _parse_uuid("not-a-uuid") is None
    assert _parse_uuid(None) is None
    assert _parse_uuid(12345) is None


async def test_timeout_yields_system_error(tmp_path: Path) -> None:
    redis = await _make_redis()
    async with memory_session() as s:
        run, account = await _seed(s)
        await s.commit()
        # A tiny timeout + a worker that never reports → TaskTimeout → system_error.
        settings = Settings(executor_task_timeout_s=0.05)
        oc = ExecutorOrchestrator(
            session=s,
            redis=redis,
            account=account,
            settings=settings,
            sandbox_manager=FakeSandboxManager(FakeBox()),
        )
        result = await oc.run(run=run, workspace_dir=tmp_path)
        await s.commit()

    assert result.outcome == "system_error"
    assert "timed out" in result.summary
    await redis.aclose()


async def test_timeout_marks_workstep_and_attempt_failed(tmp_path: Path) -> None:
    from sqlalchemy import select

    redis = await _make_redis()
    async with memory_session() as s:
        run, account = await _seed(s)
        await s.commit()
        settings = Settings(executor_task_timeout_s=0.05)
        oc = ExecutorOrchestrator(
            session=s,
            redis=redis,
            account=account,
            settings=settings,
            sandbox_manager=FakeSandboxManager(FakeBox()),
        )
        result = await oc.run(run=run, workspace_dir=tmp_path)
        await s.flush()

        assert result.outcome == "system_error"
        step = (await s.execute(select(WorkStep))).scalar_one()
        attempt = (await s.execute(select(RunAttempt))).scalar_one()
        assert step.status is WorkStepStatus.FAILED
        assert attempt.phase is RunAttemptPhase.FAILED
        assert attempt.finished_at is not None
        # No deliverable, no decision.
        assert (await s.execute(select(Decision))).first() is None
    await redis.aclose()


async def test_dispatched_task_does_not_carry_backend_absolute_path(tmp_path: Path) -> None:
    from sqlalchemy import select

    # ``tmp_path`` stands in for the backend container's /app/var/runs/<run_id>
    # path. It is meaningless to a remote worker, so the dispatched task must NOT
    # carry it — the worker manages its own per-task local dir now.
    redis = await _make_redis()
    async with memory_session() as s:
        run, account = await _seed(s)
        await s.commit()
        settings = Settings(executor_task_timeout_s=0.05)
        oc = ExecutorOrchestrator(
            session=s,
            redis=redis,
            account=account,
            settings=settings,
            sandbox_manager=FakeSandboxManager(FakeBox()),
        )
        await oc.run(run=run, workspace_dir=tmp_path)
        await s.flush()

        task = (await s.execute(select(ExecutorTaskRow))).scalar_one()
        assert task.workspace_dir != str(tmp_path)
        assert task.workspace_dir == "."
    await redis.aclose()


async def test_module_exports_decision_kinds() -> None:
    assert orch.DECISION_NO_WORKER_AVAILABLE == "no_executor_worker_available"
    assert orch.DECISION_NO_DISPATCH_TRANSPORT == "no_executor_dispatch_transport"


# --------------------------------------------------------------------------
# B2b — executor verification convergence (KILL the fake PROVED)
# --------------------------------------------------------------------------


async def test_success_no_contract_yields_human_review_not_proved(tmp_path: Path) -> None:
    """THE anti-regression: worker exits 0 but there is NO verifiable contract
    (no retriever, no declared contract) → a ``human_review_required`` Decision,
    NOT a verified Deliverable, and ``proof_state`` is NEVER set to PROVED."""
    from sqlalchemy import select

    redis = await _make_redis()
    engine, sf, base = _shared_sqlite_sessionmaker()
    async with engine.begin() as conn:
        await conn.run_sync(base.metadata.create_all)
    try:
        async with sf() as s:
            run, account = await _seed(s)
            worker_id = _parse_uuid(account.extra_params["worker_id"])
            assert worker_id is not None
            await s.commit()

        box = FakeBox()
        manager = FakeSandboxManager(box)
        async with sf() as orch_s:
            run = await orch_s.get(ExecutionRun, run.id)
            assert run is not None
            settings = Settings(executor_task_timeout_s=5.0)
            oc = ExecutorOrchestrator(
                session=orch_s,
                redis=redis,
                account=account,
                settings=settings,
                sandbox_manager=manager,
                retriever=None,  # no canon → assemble_contract returns None
                verify_llm=StubLlm([]),
            )
            result = await _drive_with_worker_done(
                oc, redis=redis, run=run, worker_id=worker_id, workspace_dir=tmp_path, sf=sf
            )
            await orch_s.commit()

        assert result.outcome == "needs_decision"

        async with sf() as s:
            decision = (await s.execute(select(Decision))).scalar_one()
            assert decision.decision == "human_review_required"
            assert decision.payload.get("reason") == "no_verifiable_contract"
            # NO verified Deliverable, NO VerificationResult, proof NOT PROVED.
            assert (await s.execute(select(Deliverable))).first() is None
            assert (await s.execute(select(VerificationResult))).first() is None
            step = (await s.execute(select(WorkStep))).scalar_one()
            assert step.proof_state is not ProofState.PROVED
            assert step.status is not WorkStepStatus.VERIFIED
        # The sandbox was acquired AND released (finally), even on the Decision branch.
        assert len(manager.acquired) == 1
        assert len(manager.released) == 1
    finally:
        await engine.dispose()
        await redis.aclose()


async def test_contract_pass_sets_proved_and_writes_deliverable(tmp_path: Path) -> None:
    """The ONLY path that sets PROVED: a runnable contract that PASSES. A canon
    command check passes in the fake box → PASSED VerificationResult → verified
    Deliverable with the real artifact_refs (B1) → proof_state PROVED."""
    from sqlalchemy import select

    redis = await _make_redis()
    engine, sf, base = _shared_sqlite_sessionmaker()
    async with engine.begin() as conn:
        await conn.run_sync(base.metadata.create_all)
    root = tmp_path / "runs"
    try:
        async with sf() as s:
            run, account = await _seed(s)
            worker_id = _parse_uuid(account.extra_params["worker_id"])
            assert worker_id is not None
            await s.commit()

        # The canon retriever yields a JUDGE criterion; the judge LLM passes it.
        box = FakeBox(files={"result.py": b"print('done')\n"})
        manager = FakeSandboxManager(box)
        retriever = StubRetriever(["the change is correct and tested"])
        judge = StubLlm([LoopTurn(content='{"passed": true, "reasoning": "ok"}')])
        async with sf() as orch_s:
            run = await orch_s.get(ExecutionRun, run.id)
            assert run is not None
            settings = Settings(executor_task_timeout_s=5.0)
            oc = ExecutorOrchestrator(
                session=orch_s,
                redis=redis,
                account=account,
                settings=settings,
                sandbox_manager=manager,
                retriever=retriever,
                verify_llm=judge,
            )
            result = await _drive_with_worker_done(
                oc,
                redis=redis,
                run=run,
                worker_id=worker_id,
                workspace_dir=tmp_path,
                sf=sf,
                output="implemented + tests green",
                files=[
                    {
                        "path": "result.py",
                        "content_b64": __import__("base64").b64encode(b"print('done')\n").decode(),
                        "truncated": False,
                    }
                ],
                run_workspace_root=str(root),
            )
            await orch_s.commit()

        assert result.outcome == "verified"
        assert result.written_paths == ["result.py"]

        async with sf() as s:
            vr = (await s.execute(select(VerificationResult))).scalar_one()
            assert vr.outcome is VerificationOutcome.PASSED
            deliverable = (await s.execute(select(Deliverable))).scalar_one()
            assert deliverable.payload.get("artifact_refs") == ["result.py"]
            assert deliverable.payload.get("summary") == "implemented + tests green"
            step = (await s.execute(select(WorkStep))).scalar_one()
            assert step.proof_state is ProofState.PROVED
            assert step.status is WorkStepStatus.VERIFIED
            assert (await s.execute(select(Decision))).first() is None
        assert len(manager.acquired) == 1
        assert len(manager.released) == 1
    finally:
        await engine.dispose()
        await redis.aclose()


async def test_contract_fail_yields_verification_failed_decision(tmp_path: Path) -> None:
    """A runnable contract that FAILS → a ``verification_failed`` Decision (NOT
    an auto-retry, NOT PROVED — executor is single-dispatch, FAIL → founder)."""
    from sqlalchemy import select

    redis = await _make_redis()
    engine, sf, base = _shared_sqlite_sessionmaker()
    async with engine.begin() as conn:
        await conn.run_sync(base.metadata.create_all)
    try:
        async with sf() as s:
            run, account = await _seed(s)
            worker_id = _parse_uuid(account.extra_params["worker_id"])
            assert worker_id is not None
            await s.commit()

        box = FakeBox()
        manager = FakeSandboxManager(box)
        retriever = StubRetriever(["must satisfy the spec"])
        judge = StubLlm([LoopTurn(content='{"passed": false, "reasoning": "nope"}')])
        async with sf() as orch_s:
            run = await orch_s.get(ExecutionRun, run.id)
            assert run is not None
            settings = Settings(executor_task_timeout_s=5.0)
            oc = ExecutorOrchestrator(
                session=orch_s,
                redis=redis,
                account=account,
                settings=settings,
                sandbox_manager=manager,
                retriever=retriever,
                verify_llm=judge,
            )
            result = await _drive_with_worker_done(
                oc, redis=redis, run=run, worker_id=worker_id, workspace_dir=tmp_path, sf=sf
            )
            await orch_s.commit()

        assert result.outcome == "needs_decision"
        async with sf() as s:
            decision = (await s.execute(select(Decision))).scalar_one()
            assert decision.decision == "verification_failed"
            # The FAILED VerificationResult was written but NO PROVED, NO Deliverable.
            vr = (await s.execute(select(VerificationResult))).scalar_one()
            assert vr.outcome is VerificationOutcome.FAILED
            assert (await s.execute(select(Deliverable))).first() is None
            step = (await s.execute(select(WorkStep))).scalar_one()
            assert step.proof_state is not ProofState.PROVED
        assert len(manager.released) == 1
    finally:
        await engine.dispose()
        await redis.aclose()


async def test_judge_contract_without_verify_llm_yields_human_review(tmp_path: Path) -> None:
    """A contract with a JUDGE check but ``verify_llm is None`` (cannot run the
    judge) → ``human_review_required`` (reason ``no_verification_llm``), NOT
    PROVED. (Executor-only-active workspace → no judge LLM resolvable.)"""
    from sqlalchemy import select

    redis = await _make_redis()
    engine, sf, base = _shared_sqlite_sessionmaker()
    async with engine.begin() as conn:
        await conn.run_sync(base.metadata.create_all)
    try:
        async with sf() as s:
            run, account = await _seed(s)
            worker_id = _parse_uuid(account.extra_params["worker_id"])
            assert worker_id is not None
            await s.commit()

        box = FakeBox()
        manager = FakeSandboxManager(box)
        retriever = StubRetriever(["the diff matches the canonical pattern"])  # → judge check
        async with sf() as orch_s:
            run = await orch_s.get(ExecutionRun, run.id)
            assert run is not None
            settings = Settings(executor_task_timeout_s=5.0)
            oc = ExecutorOrchestrator(
                session=orch_s,
                redis=redis,
                account=account,
                settings=settings,
                sandbox_manager=manager,
                retriever=retriever,
                verify_llm=None,  # cannot run the judge
            )
            result = await _drive_with_worker_done(
                oc, redis=redis, run=run, worker_id=worker_id, workspace_dir=tmp_path, sf=sf
            )
            await orch_s.commit()

        assert result.outcome == "needs_decision"
        async with sf() as s:
            decision = (await s.execute(select(Decision))).scalar_one()
            assert decision.decision == "human_review_required"
            assert decision.payload.get("reason") == "no_verification_llm"
            assert (await s.execute(select(Deliverable))).first() is None
            assert (await s.execute(select(VerificationResult))).first() is None
            step = (await s.execute(select(WorkStep))).scalar_one()
            assert step.proof_state is not ProofState.PROVED
        assert len(manager.released) == 1
    finally:
        await engine.dispose()
        await redis.aclose()


async def test_b2b_decision_kind_exports() -> None:
    assert orch.DECISION_HUMAN_REVIEW_REQUIRED == "human_review_required"
    assert orch.DECISION_VERIFICATION_FAILED == "verification_failed"


# --------------------------------------------------------------------------
# B8 — context assembly for executor dispatch (CLI parity)
#
# The dispatch now ships a NON-empty engineer system prompt + a context-rich
# framed prompt (intent + relevant canon + founder-resolved decisions) instead
# of the bare 512-char intent with an EMPTY system. These assert the created
# ExecutorTaskRow carries them; the dispatch→await→verify flow is unchanged.
# --------------------------------------------------------------------------


async def _create_task_only(
    s: Any,
    *,
    redis: Any,
    account: ModelAccount,
    run: ExecutionRun,
    tmp_path: Path,
    retriever: Any | None = None,
) -> ExecutorTaskRow:
    """Drive ``oc.run`` with a 0.05s timeout (no worker reports) so it dispatches
    a task then fails on timeout — the task row carries the assembled prompt."""
    from sqlalchemy import select

    settings = Settings(executor_task_timeout_s=0.05)
    oc = ExecutorOrchestrator(
        session=s,
        redis=redis,
        account=account,
        settings=settings,
        sandbox_manager=FakeSandboxManager(FakeBox()),
        retriever=retriever,
    )
    await oc.run(run=run, workspace_dir=tmp_path)
    await s.flush()
    return (await s.execute(select(ExecutorTaskRow))).scalar_one()


async def test_dispatch_carries_non_empty_system_prompt(tmp_path: Path) -> None:
    redis = await _make_redis()
    async with memory_session() as s:
        run, account = await _seed(s)
        await s.commit()
        task = await _create_task_only(s, redis=redis, account=account, run=run, tmp_path=tmp_path)
        # Was "" before B8 — now a real engineer system prompt.
        assert task.system != ""
        assert "engineer" in task.system.lower()
    await redis.aclose()


async def test_dispatch_prompt_includes_canon_and_decisions(tmp_path: Path) -> None:
    redis = await _make_redis()
    async with memory_session() as s:
        run, account = await _seed(s)
        run.payload = {
            "intent_text": "ship the parser",
            "resolved_decisions": [
                {"decision_id": "d1", "question": "Strict mode?", "answer": "Yes, strict"},
            ],
        }
        await s.flush()
        await s.commit()
        retriever = StubRetriever(["parsers belong in backend/parse/"])
        task = await _create_task_only(
            s, redis=redis, account=account, run=run, tmp_path=tmp_path, retriever=retriever
        )
        assert "ship the parser" in task.prompt
        assert "Relevant established patterns" in task.prompt
        assert "parsers belong in backend/parse/" in task.prompt
        assert "The founder resolved" in task.prompt
        assert "Strict mode?" in task.prompt
        assert "Yes, strict" in task.prompt
        # The retriever was queried with the run's intent (same signal as B6).
        assert retriever.queried == ["ship the parser"]
    await redis.aclose()


async def test_dispatch_prompt_intent_only_when_empty_knowledge(tmp_path: Path) -> None:
    redis = await _make_redis()
    async with memory_session() as s:
        run, account = await _seed(s)  # payload = {"intent_text": "do work"}, no decisions
        await s.commit()
        # No retriever → no canon; no resolved_decisions → no decisions section.
        task = await _create_task_only(
            s, redis=redis, account=account, run=run, tmp_path=tmp_path, retriever=None
        )
        assert "do work" in task.prompt
        assert "Relevant established patterns" not in task.prompt
        assert "The founder resolved" not in task.prompt
        # System prompt is still shipped even with empty knowledge.
        assert task.system != ""
    await redis.aclose()


async def test_dispatch_prompt_graceful_when_retriever_raises(tmp_path: Path) -> None:
    class _BoomRetriever:
        async def retrieve_for_signals(self, signals: str) -> list[str]:
            raise RuntimeError("canon backend down")

    redis = await _make_redis()
    async with memory_session() as s:
        run, account = await _seed(s)
        await s.commit()
        # A retriever that raises must never crash dispatch — degrade to intent-only.
        task = await _create_task_only(
            s, redis=redis, account=account, run=run, tmp_path=tmp_path, retriever=_BoomRetriever()
        )
        assert "do work" in task.prompt
        assert "Relevant established patterns" not in task.prompt
        assert task.system != ""
    await redis.aclose()
