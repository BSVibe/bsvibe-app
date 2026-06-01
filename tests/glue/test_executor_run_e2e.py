"""Executor run end-to-end — provider='executor' run dispatches to a worker.

Lift 5b of the executor-pool epic (Workflow §8.4 / §11.3). The KEYSTONE
integration: a run whose resolved ModelAccount is ``provider='executor'``
must NOT enter the native LLM loop — it must dispatch a task to a registered
external worker and, on the worker reporting success, produce the SAME
verified artifacts the native path produces (Deliverable type CODE +
DeliveryEventRow + settle activity), landing the run REVIEW_READY.

This drives the *real* :func:`backend.workflow.infrastructure.workers.run._factory` branch (so the
provider switch + ExecutorOrchestrator construction are exercised, not a
hand-built orchestrator) through :meth:`AgentRunner.drive`, and SIMULATES
the worker with ``fakeredis`` + :func:`dispatch.record_result` + a publish on
the done channel — exactly the shape of ``tests/executors/test_dispatch.py``.

Runs on in-memory SQLite by default, real Postgres when ``BSVIBE_DATABASE_URL``
is set (mirrors the other glue tests).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Importing the module dbs registers their tables on the shared Base.metadata.
import backend.executors.db  # noqa: F401
from backend.config import get_settings
from backend.executors import dispatch
from backend.executors.db import WorkerRow
from backend.executors.orchestrator import ExecutorOrchestrator
from backend.router.accounts.models import ModelAccount
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.infrastructure.db import (
    Decision,
    Deliverable,
    DeliverableType,
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow
from backend.workflow.infrastructure.workers.run import build_agent_execution_deps

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


def _short_timeout_settings(timeout_s: float = 30.0):
    """Settings with a SHORT ``executor_task_timeout_s`` for the happy/failure
    e2e paths. The prod default is 1800s (30 min): if the done publish is ever
    raced/missed, ``await_completion`` would block on that timeout before the DB
    fallback — a 30-minute test hang. A few-second cap keeps the test fast while
    still exercising the real await/fallback path. Threaded through
    :func:`build_agent_execution_deps` into the per-run ExecutorOrchestrator.

    Bumped from 5s → 30s: under CI load with B15 audit emits + B16 SSE bridge
    overhead the worker-done simulation race could land past 5s and trip
    TaskTimeout → system_error, masking the actual outcome the test asserts."""
    return get_settings().model_copy(update={"executor_task_timeout_s": timeout_s})


async def _await_dispatched_task_id(redis: Any, *, worker_id: uuid.UUID) -> uuid.UUID:
    """Block until the orchestrator XADDs a task onto ``worker_id``'s stream;
    return the dispatched ``task_id``.

    This is how the REAL worker daemon learns of a task — a remote machine reads
    the Redis stream XADD, never the orchestrator's ``executor_tasks`` DB row.
    Driving the simulated worker off the stream (the production dispatch signal)
    keeps it faithful on both backends. It also sidesteps the original e2e bug:
    polling the DB for the ``dispatched`` row happened to work on SQLite (the
    StaticPool shares one connection so an UNCOMMITTED row was visible) but timed
    out on real PG (READ COMMITTED hides another session's uncommitted writes).
    The orchestrator now commits the dispatched task before awaiting, so the
    worker's separate ``record_result`` session can find + flip it terminal."""
    stream = dispatch.worker_stream(worker_id)
    last_id = "0"
    for _ in range(500):
        entries = await redis.xread({stream: last_id}, count=1, block=20)
        if not entries:
            continue
        _stream_name, messages = entries[0]
        for msg_id, fields in messages:
            last_id = msg_id
            return uuid.UUID(fields["task_id"])
    raise AssertionError(f"no task dispatched onto {stream}")


async def _make_redis() -> Any:
    try:
        import fakeredis
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:  # pragma: no cover - fakeredis is a declared dep
        pytest.skip("fakeredis not installed")
    # Isolated server per instance: the default shared server binds its async
    # primitives to the first event loop that touches it, so reuse across
    # pytest-asyncio's per-test loops raises cross-loop Future errors.
    client = fakeredis_aio.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    await client.flushdb()
    return client


async def _seed_worker(
    s: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    capabilities: list[str],
) -> WorkerRow:
    worker = WorkerRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name="mac-mini",
        labels=[],
        capabilities=list(capabilities),
        status="online",
        last_heartbeat=datetime.now(UTC) - timedelta(seconds=1),
        token_hash="0" * 64,
        is_active=True,
    )
    s.add(worker)
    await s.flush()
    return worker


async def _seed_executor_account(
    s: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    worker_id: uuid.UUID,
    executor_type: str,
) -> ModelAccount:
    account = ModelAccount(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        account_id=uuid.uuid4(),
        provider="executor",
        label="mac-mini",
        litellm_model=f"executor/{executor_type}",
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="unknown",
        is_active=True,
        extra_params={"worker_id": str(worker_id), "executor_type": executor_type},
    )
    s.add(account)
    await s.flush()
    return account


async def _open_run(s: AsyncSession, *, workspace_id: uuid.UUID, text: str) -> uuid.UUID:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.OPEN,
        payload={"intent_text": text},
    )
    s.add(run)
    await s.flush()
    return run.id


async def _open_run_with_payload(
    s: AsyncSession, *, workspace_id: uuid.UUID, payload: dict[str, Any]
) -> uuid.UUID:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.OPEN,
        payload=payload,
    )
    s.add(run)
    await s.flush()
    return run.id


# --------------------------------------------------------------------------
# B2b verify-convergence doubles — a scripted sandbox + judge LLM + retriever
# so the verified-PASS path can be exercised end-to-end through AgentRunner.
# --------------------------------------------------------------------------


class _FakeBox:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self._files = files or {}

    @property
    def workspace_mount(self) -> str:
        return "/work"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False):
        from backend.workflow.infrastructure.sandbox.protocol import SandboxResult  # noqa: PLC0415

        return SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        return self._files.get(rel_path, b"")

    async def write_file(self, rel_path: str, content: bytes) -> None:  # pragma: no cover
        self._files[rel_path] = content

    async def list_dir(self, rel_path: str) -> list[str]:  # pragma: no cover
        return list(self._files)


class _FakeSandboxManager:
    def __init__(self, box: _FakeBox) -> None:
        self._box = box
        self.acquired = 0
        self.released = 0

    async def acquire(self, project_id: uuid.UUID, workspace_path: str) -> _FakeBox:
        self.acquired += 1
        return self._box

    async def release(self, project_id: uuid.UUID) -> None:
        self.released += 1

    async def reap_idle(self) -> None:  # pragma: no cover
        return None

    async def health(self) -> bool:  # pragma: no cover
        return True


class _StubJudge:
    def __init__(self, passed: bool) -> None:
        self._passed = passed

    async def complete(self, *, messages: list[dict[str, Any]], tools: Any):
        from backend.workflow.application.agent_loop import LoopTurn  # noqa: PLC0415

        verdict = "true" if self._passed else "false"
        return LoopTurn(content=f'{{"passed": {verdict}, "reasoning": "x"}}')


class _StubRetriever:
    def __init__(self, patterns: list[str]) -> None:
        self._patterns = patterns

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        return list(self._patterns)


async def _simulate_worker_done(
    redis: Any,
    *,
    worker_id: uuid.UUID,
    sf: async_sessionmaker[AsyncSession],
    output: str,
    files: list[dict[str, Any]] | None = None,
    run_workspace_root: str | None = None,
) -> None:
    """The standard simulated-worker coroutine: learn the task from the stream
    XADD, then report a ``done`` result on a SEPARATE session."""
    task_id = await _await_dispatched_task_id(redis, worker_id=worker_id)
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


# --------------------------------------------------------------------------
# 1. KEYSTONE (B2b): through the REAL production factory (retriever=None today,
#    no judge account) a successful executor run produces NO verifiable
#    contract → human-review Decision, NOT a fake-PROVED verified Deliverable.
#    This is the anti-regression for the fake-PROVED sin.
# --------------------------------------------------------------------------


async def test_executor_run_success_no_contract_routes_to_human_review(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    executor_type = "claude_code"

    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=[executor_type])
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type=executor_type
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="ship the feature")
        await s.commit()

    # The real production factory branches on provider == "executor" and builds
    # an ExecutorOrchestrator. Today it wires retriever=None (B3 wires canon),
    # and the workspace has only an executor account (no judge LLM) — so a
    # successful worker exit assembles NO contract and routes to human review.
    deps = build_agent_execution_deps(redis_client=redis, settings=_short_timeout_settings())

    async with sf() as orch_s:
        run = await orch_s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(orch_s, run)
        assert isinstance(orchestrator, ExecutorOrchestrator)

        runner = AgentRunner(orch_s)
        drive_task = asyncio.create_task(
            runner.drive(run_id=run_id, orchestrator=orchestrator, workspace_dir=tmp_path)
        )
        worker_task = asyncio.create_task(
            _simulate_worker_done(
                redis, worker_id=worker.id, sf=sf, output="implemented + tests green"
            )
        )
        result = await drive_task
        await worker_task
        await orch_s.commit()

    # NOT verified — exit-0 with no checkable contract is NOT a verified deliverable.
    assert result.outcome == "needs_decision"

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        # needs_decision leaves the run RUNNING (paused on the Decision).
        assert run is not None and run.status is RunStatus.RUNNING

        decision = (await s.execute(select(Decision))).scalar_one()
        assert decision.decision == "human_review_required"
        assert decision.payload.get("reason") == "no_verifiable_contract"

        # NO fake-PROVED Deliverable / DeliveryEvent / settle were written.
        assert (await s.execute(select(Deliverable))).first() is None
        assert (await s.execute(select(DeliveryEventRow))).first() is None
        settle = (
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.activity_type == "settle"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert settle == []

        task = (
            await s.execute(
                select(dispatch.ExecutorTaskRow).where(
                    dispatch.ExecutorTaskRow.workspace_id == workspace_id
                )
            )
        ).scalar_one()
        assert task.status == "done"
        assert "ship the feature" in task.prompt

    await redis.aclose()


# --------------------------------------------------------------------------
# 1a. KEYSTONE PASS (B2b): a runnable contract that PASSES → verified terminal.
#     Constructs the ExecutorOrchestrator directly with a fake sandbox + canon
#     retriever + passing judge (the seams B3/judge-account wire in prod) and
#     drives it through AgentRunner → REVIEW_READY + a REAL verified Deliverable.
# --------------------------------------------------------------------------


async def test_executor_run_contract_pass_verifies_and_review_ready(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    executor_type = "claude_code"

    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=[executor_type])
        account = await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type=executor_type
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="ship the feature")
        await s.commit()

    async with sf() as orch_s:
        run = await orch_s.get(ExecutionRun, run_id)
        assert run is not None
        manager = _FakeSandboxManager(_FakeBox(files={"result.py": b"print('done')\n"}))
        orchestrator = ExecutorOrchestrator(
            session=orch_s,
            redis=redis,
            account=account,
            settings=_short_timeout_settings(),
            sandbox_manager=manager,
            retriever=_StubRetriever(["the change is correct and tested"]),
            verify_llm=_StubJudge(passed=True),
        )

        runner = AgentRunner(orch_s)
        drive_task = asyncio.create_task(
            runner.drive(run_id=run_id, orchestrator=orchestrator, workspace_dir=tmp_path)
        )
        worker_task = asyncio.create_task(
            _simulate_worker_done(redis, worker_id=worker.id, sf=sf, output="implemented + green")
        )
        result = await drive_task
        await worker_task
        await orch_s.commit()

    assert result.outcome == "verified"

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.REVIEW_READY

        # PROVED was gated on a real PASSED VerificationResult.
        vr = (await s.execute(select(VerificationResult))).scalar_one()
        assert vr.outcome is VerificationOutcome.PASSED

        deliverable = (await s.execute(select(Deliverable))).scalar_one()
        assert deliverable.deliverable_type is DeliverableType.CODE
        assert deliverable.payload.get("summary") == "implemented + green"

        deliver_event = (await s.execute(select(DeliveryEventRow))).scalar_one()
        assert deliver_event.deliverable_id == deliverable.id

        settle = (
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.run_id == run_id,
                        ExecutionRunActivity.activity_type == "settle",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(settle) == 1
        assert settle[0].payload.get("verified") is True

        # W1: the L-P2 ``ship_or_discard`` synthesis is retired. Verified
        # runs no longer get a synthetic Decision on REVIEW_READY — W2
        # wires auto-merge instead. The executor verified-PASS path mints
        # no Decision of its own, so the count is 0.
        assert (await s.execute(select(Decision))).first() is None

    await redis.aclose()


# --------------------------------------------------------------------------
# 1b. KEY B1 DELTA + B2b: worker-produced file lands as a real artifact_ref on
#     the verified Deliverable and ROUND-TRIPS through the artifact-read
#     endpoint. Drives the B2b verified-PASS path (fake sandbox + retriever +
#     passing judge) so a real verified Deliverable is written — the Deliverable
#     is gated on a passing VerificationResult, never fake-PROVED.
# --------------------------------------------------------------------------


async def test_executor_run_captures_artifact_and_serves_via_endpoint(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    import httpx

    from backend.api.deps import get_current_user, get_db_session, get_workspace_id
    from backend.api.main import create_app
    from backend.config import get_settings

    from .._support import fake_current_user

    # Point the run workspace root at a tmp dir (where captured files persist +
    # where the artifact endpoint reads them back from).
    root = tmp_path / "runs"
    monkeypatch.setenv("BSVIBE_RUN_WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()

    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    executor_type = "claude_code"

    try:
        async with sf() as s:
            worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=[executor_type])
            account = await _seed_executor_account(
                s, workspace_id=workspace_id, worker_id=worker.id, executor_type=executor_type
            )
            run_id = await _open_run(s, workspace_id=workspace_id, text="ship the feature")
            await s.commit()

        settings = get_settings().model_copy(update={"executor_task_timeout_s": 30.0})

        async with sf() as orch_s:
            run = await orch_s.get(ExecutionRun, run_id)
            assert run is not None
            # B2b verified-PASS seams: a fake sandbox + canon retriever + a
            # passing judge → a real PASSED VerificationResult → verified
            # Deliverable carrying the captured artifact_ref (B1).
            orchestrator = ExecutorOrchestrator(
                session=orch_s,
                redis=redis,
                account=account,
                settings=settings,
                sandbox_manager=_FakeSandboxManager(_FakeBox()),
                retriever=_StubRetriever(["the change is correct"]),
                verify_llm=_StubJudge(passed=True),
            )

            runner = AgentRunner(orch_s)
            drive_task = asyncio.create_task(
                runner.drive(run_id=run_id, orchestrator=orchestrator, workspace_dir=tmp_path)
            )
            worker_task = asyncio.create_task(
                _simulate_worker_done(
                    redis,
                    worker_id=worker.id,
                    sf=sf,
                    output="implemented",
                    files=[
                        {
                            "path": "result.py",
                            "content_b64": base64.b64encode(b"print('done')\n").decode(),
                            "truncated": False,
                        }
                    ],
                    run_workspace_root=str(root),
                )
            )
            result = await drive_task
            await worker_task
            await orch_s.commit()

        assert result.outcome == "verified"
        assert result.written_paths == ["result.py"]

        async with sf() as s:
            deliverable = (await s.execute(select(Deliverable))).scalar_one()
            deliverable_id = deliverable.id
            # The KEY delta: artifact_refs is NON-EMPTY (was always [] before B1).
            assert deliverable.payload.get("artifact_refs") == ["result.py"]

        # The captured file persisted under the run dir.
        assert (root / str(run_id) / "result.py").read_bytes() == b"print('done')\n"

        # ROUND-TRIP: the EXISTING artifact-read endpoint serves the content
        # (no endpoint change — persisting real refs is all it took).
        app = create_app()

        async def _session():
            async with sf() as s:
                yield s

        app.dependency_overrides[get_db_session] = _session
        app.dependency_overrides[get_current_user] = fake_current_user()
        app.dependency_overrides[get_workspace_id] = lambda: workspace_id
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(f"/api/v1/deliverables/{deliverable_id}/artifacts/result.py")
            assert r.status_code == 200, r.text
            assert r.json()["content"] == "print('done')\n"
    finally:
        get_settings.cache_clear()
        await redis.aclose()


# --------------------------------------------------------------------------
# 2. No worker available → Decision, run stays RUNNING (needs_decision)
# --------------------------------------------------------------------------


async def test_executor_run_no_worker_creates_decision(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()

    async with sf() as s:
        # Account exists but NO online worker carries the capability.
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=uuid.uuid4(), executor_type="claude_code"
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="do the thing")
        await s.commit()

    deps = build_agent_execution_deps(redis_client=redis)
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, ExecutorOrchestrator)
        runner = AgentRunner(s)
        result = await runner.drive(
            run_id=run_id, orchestrator=orchestrator, workspace_dir=tmp_path
        )
        await s.commit()

    assert result.outcome == "needs_decision"
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.RUNNING
        decisions = (await s.execute(select(Decision))).scalars().all()
        assert len(decisions) == 1
        assert decisions[0].run_id == run_id
        # No deliverable produced.
        assert (await s.execute(select(Deliverable))).first() is None

    await redis.aclose()


# --------------------------------------------------------------------------
# 3. Worker reports failure → system_error → run FAILED
# --------------------------------------------------------------------------


async def test_executor_run_worker_failure_fails_run(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    executor_type = "codex"

    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=[executor_type])
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type=executor_type
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="ship it")
        await s.commit()

    deps = build_agent_execution_deps(redis_client=redis, settings=_short_timeout_settings())
    async with sf() as orch_s:
        run = await orch_s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(orch_s, run)

        # Same separate-session + stream-driven contract as the happy path — the
        # worker reports failure on its OWN session (concurrent flushes on a
        # shared AsyncSession collide) and learns the task from the stream XADD.
        async def _simulate_failing_worker() -> None:
            task_id = await _await_dispatched_task_id(redis, worker_id=worker.id)
            async with sf() as worker_s:
                # record_result records + publishes the done channel itself.
                await dispatch.record_result(
                    worker_s,
                    redis,
                    task_id=task_id,
                    success=False,
                    output="",
                    error_message="cli exited 1",
                )
                await worker_s.commit()

        runner = AgentRunner(orch_s)
        drive_task = asyncio.create_task(
            runner.drive(run_id=run_id, orchestrator=orchestrator, workspace_dir=tmp_path)
        )
        worker_task = asyncio.create_task(_simulate_failing_worker())
        result = await drive_task
        await worker_task
        await orch_s.commit()

    assert result.outcome == "system_error"
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.FAILED
        assert (await s.execute(select(Deliverable))).first() is None

    await redis.aclose()


# --------------------------------------------------------------------------
# 4. Non-executor (api-llm) account still builds the native RunOrchestrator
# --------------------------------------------------------------------------


async def test_non_executor_account_builds_native_orchestrator(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from backend.config import get_settings as _get_settings
    from backend.router.llm_client import LlmClient
    from backend.workflow.application.agent_loop import RunOrchestrator
    from backend.workflow.application.runtime import dispatcher as runtime_dispatcher
    from backend.workflow.infrastructure.workers import (
        run as run_module,  # noqa: F401 — legacy alias
    )

    # The native path eagerly builds the credential cipher (to decrypt the
    # account's api key) — provide a test KMS key so it constructs. It also
    # builds ``LlmClient()`` which lazily imports litellm (not a declared dep);
    # patch it to a no-op client so the smoke test exercises the *branch* (native
    # RunOrchestrator built, not ExecutorOrchestrator) without a real LLM dep.
    # Lift §17.2a: LlmClient lookup moved to runtime.dispatcher.
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    _get_settings.cache_clear()
    monkeypatch.setattr(
        runtime_dispatcher, "LlmClient", lambda: LlmClient(completion_fn=lambda **_: None)
    )

    workspace_id = uuid.uuid4()
    async with sf() as s:
        account = ModelAccount(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            account_id=uuid.uuid4(),
            provider="anthropic",
            label="claude",
            litellm_model="claude-3-5-sonnet",
            api_base=None,
            api_key_encrypted="ciphertext",
            data_jurisdiction="us",
            is_active=True,
            extra_params={},
        )
        s.add(account)
        run_id = await _open_run(s, workspace_id=workspace_id, text="native run")
        await s.commit()

    deps = build_agent_execution_deps()
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, RunOrchestrator)
        assert not isinstance(orchestrator, ExecutorOrchestrator)


# --------------------------------------------------------------------------
# 5. Executor account but no redis client → cannot dispatch → Decision
# --------------------------------------------------------------------------


async def test_executor_run_without_redis_creates_decision(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = uuid.uuid4()
    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=["claude_code"])
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type="claude_code"
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="no redis here")
        await s.commit()

    deps = build_agent_execution_deps()  # no redis_client
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, ExecutorOrchestrator)
        runner = AgentRunner(s)
        result = await runner.drive(
            run_id=run_id, orchestrator=orchestrator, workspace_dir=tmp_path
        )
        await s.commit()

    assert result.outcome == "needs_decision"
    async with sf() as s:
        decisions = (await s.execute(select(Decision))).scalars().all()
        assert len(decisions) == 1


# --------------------------------------------------------------------------
# 6. Timeout setting default sanity
# --------------------------------------------------------------------------


async def test_executor_task_timeout_setting_default() -> None:
    assert get_settings().executor_task_timeout_s == 1800.0


# --------------------------------------------------------------------------
# 7. B3 — _factory injects a REAL (non-None) CanonRetriever into BOTH
#    orchestrators. Prior state: retriever was ALWAYS None in prod (RC-2), so
#    BSage canon was never folded into verification. The delta asserted here is
#    None → a workspace-scoped retriever on each orchestrator.
# --------------------------------------------------------------------------


def _vault_root_settings(tmp_path: Path, timeout_s: float = 30.0):
    """Settings pointing the knowledge vault root at ``tmp_path`` (so the
    factory's retriever reads the test-seeded canon) + a short executor timeout."""
    return get_settings().model_copy(
        update={
            "knowledge_vault_root": str(tmp_path / "vault"),
            "executor_task_timeout_s": timeout_s,
        }
    )


async def _seed_canon_concept(
    *,
    vault_root: Path,
    region: str,
    workspace_id: uuid.UUID,
    concept_id: str,
    display: str,
) -> None:
    from backend.knowledge.canonicalization import models  # noqa: PLC0415
    from backend.knowledge.canonicalization.store import NoteStore  # noqa: PLC0415
    from backend.knowledge.graph.storage import FileSystemStorage  # noqa: PLC0415

    store = NoteStore(FileSystemStorage(vault_root / region / str(workspace_id)))
    await store.write_concept(
        models.ConceptEntry(
            concept_id=concept_id,
            path=f"concepts/active/{concept_id}.md",
            display=display,
            aliases=[],
            created_at=datetime(2026, 5, 6, tzinfo=UTC),
            updated_at=datetime(2026, 5, 6, tzinfo=UTC),
        )
    )


async def test_factory_wires_retriever_into_executor_orchestrator(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """B3 delta: the production factory now passes a non-None retriever to the
    ExecutorOrchestrator (was None)."""
    settings = _vault_root_settings(tmp_path)
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    await _seed_canon_concept(
        vault_root=Path(settings.knowledge_vault_root),
        region=settings.knowledge_default_region,
        workspace_id=workspace_id,
        concept_id="dependency-pinning",
        display="Always pin dependency versions",
    )
    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=["claude_code"])
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type="claude_code"
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="ship it")
        await s.commit()

    deps = build_agent_execution_deps(redis_client=redis, settings=settings)
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, ExecutorOrchestrator)
        # The delta: a real retriever is wired (NOT None as before B3).
        assert orchestrator._retriever is not None  # noqa: SLF001 — wiring invariant
        patterns = await orchestrator._retriever.retrieve_for_signals(  # noqa: SLF001
            "updated dependency pinning\nrequirements.txt"
        )
        assert "Always pin dependency versions" in patterns

    await redis.aclose()


async def test_factory_wires_retriever_into_native_orchestrator(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B3 delta: the production factory now passes a non-None retriever to the
    native RunOrchestrator (was None)."""
    import base64

    from backend.config import get_settings as _get_settings
    from backend.router.llm_client import LlmClient
    from backend.workflow.application.agent_loop import RunOrchestrator
    from backend.workflow.application.runtime import dispatcher as runtime_dispatcher
    from backend.workflow.infrastructure.workers import (
        run as run_module,  # noqa: F401 — legacy alias
    )

    # Lift §17.2a: LlmClient lookup moved to runtime.dispatcher.
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    _get_settings.cache_clear()
    monkeypatch.setattr(
        runtime_dispatcher, "LlmClient", lambda: LlmClient(completion_fn=lambda **_: None)
    )

    settings = _vault_root_settings(tmp_path)
    workspace_id = uuid.uuid4()
    await _seed_canon_concept(
        vault_root=Path(settings.knowledge_vault_root),
        region=settings.knowledge_default_region,
        workspace_id=workspace_id,
        concept_id="structured-logging",
        display="Use structlog for structured logging",
    )
    async with sf() as s:
        account = ModelAccount(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            account_id=uuid.uuid4(),
            provider="anthropic",
            label="claude",
            litellm_model="claude-3-5-sonnet",
            api_base=None,
            api_key_encrypted="ciphertext",
            data_jurisdiction="us",
            is_active=True,
            extra_params={},
        )
        s.add(account)
        run_id = await _open_run(s, workspace_id=workspace_id, text="native run")
        await s.commit()

    deps = build_agent_execution_deps(settings=settings)
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, RunOrchestrator)
        # The delta: a real retriever is wired (NOT None as before B3).
        assert orchestrator._retriever is not None  # noqa: SLF001 — wiring invariant
        patterns = await orchestrator._retriever.retrieve_for_signals(  # noqa: SLF001
            "added structured logging throughout\napp.py"
        )
        assert "Use structlog for structured logging" in patterns


# --------------------------------------------------------------------------
# D1b — the DESIGN stage of a design_then_impl pipeline must be TOLD to write a
# spec, not finished code. The integration delta: drive a real design-stage run
# through the production ExecutorOrchestrator dispatch and assert the dispatched
# task's prompt carries the spec-only directive — while an impl-stage run's
# prompt does NOT (it implements the spec). This is the no-op fix at the boundary
# being changed: design produces a SPEC, impl implements it; they are distinct.
# --------------------------------------------------------------------------


async def _dispatched_task_prompt(
    sf: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    redis: Any,
    worker_id: uuid.UUID,
    run_id: uuid.UUID,
    tmp_path: Path,
) -> str:
    """Drive ``run_id`` through the production ExecutorOrchestrator (via the real
    factory) + a simulated worker, then return the prompt the orchestrator
    dispatched for that run's task — the exact text the CLI engineer receives."""
    deps = build_agent_execution_deps(redis_client=redis, settings=_short_timeout_settings())
    async with sf() as orch_s:
        run = await orch_s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(orch_s, run)
        assert isinstance(orchestrator, ExecutorOrchestrator)
        runner = AgentRunner(orch_s)
        drive_task = asyncio.create_task(
            runner.drive(run_id=run_id, orchestrator=orchestrator, workspace_dir=tmp_path)
        )
        worker_task = asyncio.create_task(
            _simulate_worker_done(redis, worker_id=worker_id, sf=sf, output="done")
        )
        await drive_task
        await worker_task
        await orch_s.commit()

    async with sf() as s:
        task = (
            await s.execute(
                select(dispatch.ExecutorTaskRow).where(dispatch.ExecutorTaskRow.run_id == run_id)
            )
        ).scalar_one()
        return task.prompt


async def test_design_stage_dispatch_prompt_carries_spec_only_directive(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from backend.executors.orchestrator import _DESIGN_SPEC_DIRECTIVE

    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    executor_type = "claude_code"
    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=[executor_type])
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type=executor_type
        )
        # First run of a design_then_impl pipeline: no explicit stage → DESIGN.
        run_id = await _open_run_with_payload(
            s,
            workspace_id=workspace_id,
            payload={
                "intent_text": "build a JSON-backed key/value store with a typed client",
                "frame": {"pipeline": "design_then_impl"},
            },
        )
        await s.commit()

    prompt = await _dispatched_task_prompt(
        sf,
        workspace_id=workspace_id,
        redis=redis,
        worker_id=worker.id,
        run_id=run_id,
        tmp_path=tmp_path,
    )
    # The DESIGN run is told to spec, not build (the no-op root-cause fix).
    assert _DESIGN_SPEC_DIRECTIVE in prompt
    assert "key/value store" in prompt
    await redis.aclose()


async def test_impl_stage_dispatch_prompt_has_no_spec_only_directive(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from backend.executors.orchestrator import _DESIGN_SPEC_DIRECTIVE

    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    executor_type = "claude_code"
    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=[executor_type])
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type=executor_type
        )
        # The spawned IMPL run carries stage="impl" — it implements the spec.
        run_id = await _open_run_with_payload(
            s,
            workspace_id=workspace_id,
            payload={
                "intent_text": "build a JSON-backed key/value store with a typed client",
                "frame": {"pipeline": "design_then_impl"},
                "stage": "impl",
            },
        )
        await s.commit()

    prompt = await _dispatched_task_prompt(
        sf,
        workspace_id=workspace_id,
        redis=redis,
        worker_id=worker.id,
        run_id=run_id,
        tmp_path=tmp_path,
    )
    # The IMPL run must NOT be told to spec — it builds.
    assert _DESIGN_SPEC_DIRECTIVE not in prompt
    await redis.aclose()


async def test_single_pipeline_dispatch_prompt_has_no_spec_only_directive(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from backend.executors.orchestrator import _DESIGN_SPEC_DIRECTIVE

    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    executor_type = "claude_code"
    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=[executor_type])
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type=executor_type
        )
        run_id = await _open_run_with_payload(
            s,
            workspace_id=workspace_id,
            payload={"intent_text": "ship the feature", "frame": {"pipeline": "single"}},
        )
        await s.commit()

    prompt = await _dispatched_task_prompt(
        sf,
        workspace_id=workspace_id,
        redis=redis,
        worker_id=worker.id,
        run_id=run_id,
        tmp_path=tmp_path,
    )
    assert _DESIGN_SPEC_DIRECTIVE not in prompt
    await redis.aclose()


async def test_factory_retriever_empty_workspace_folds_nothing(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """B3 graceful-empty: an empty-knowledge workspace's retriever yields [] →
    no canon folded → contract unchanged (no verify behaviour change)."""
    from backend.workflow.application.verification_service import VerificationService

    settings = _vault_root_settings(tmp_path)
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=["claude_code"])
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type="claude_code"
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="ship it")
        await s.commit()

    deps = build_agent_execution_deps(redis_client=redis, settings=settings)
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, ExecutorOrchestrator)
        retriever = orchestrator._retriever  # noqa: SLF001
        assert retriever is not None
        # Empty workspace → no patterns → assemble_contract folds NO canon.
        svc = VerificationService(session=s, llm=_StubJudge(passed=True), retriever=retriever)
        contract = await svc.assemble_contract(
            declared_contract=None, written_paths=["x.py"], final_text="did a thing"
        )
        # No declared checks + no canon → None (unchanged from no-retriever).
        assert contract is None

    await redis.aclose()
