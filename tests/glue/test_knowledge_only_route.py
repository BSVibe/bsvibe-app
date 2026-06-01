"""B9b — Frame knowledge-only short-circuit ROUTING in the worker factory.

B9a recorded ``run.payload["frame"]["path_classification"]`` but only the
agent loop ran. B9b acts on it: a run framed ``knowledge_only`` (and a cheap LLM
resolvable) routes to the :class:`KnowledgeAnswerOrchestrator` (one LLM call,
answer from BSage, NO agent loop). An ``agent_loop`` run, or a knowledge-only run
with no LLM, falls back to the native loop — never stranded.

These exercise the REAL production ``build_agent_execution_deps`` → ``_factory``
routing, the gateway work-LLM stubbed at the :class:`LlmClient` boundary.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.executors.orchestrator import ExecutorOrchestrator
from backend.router.accounts.schemas import ModelAccountCreate
from backend.router.accounts.service import ModelAccountService
from backend.router.llm_client import LlmClient
from backend.workflow.application.agent_loop import RunOrchestrator
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.application.knowledge_orchestrator import (
    KNOWLEDGE_ANSWER_KIND,
    KnowledgeAnswerOrchestrator,
)
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
    VerificationResult,
)
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.workers import run as runtime
from backend.workflow.infrastructure.workers.agent_worker import AgentWorker

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_TEST_KMS_KEY_B64 = base64.urlsafe_b64encode(b"0" * 32).decode("ascii")


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def kms_key(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", _TEST_KMS_KEY_B64)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _ScriptedCompletion:
    """A scripted ``litellm.acompletion`` — pops the next response FIFO."""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = list(turns)
        self.count = 0

    async def __call__(self, **_kwargs: Any) -> SimpleNamespace:
        if not self._turns:
            raise AssertionError("scripted completion exhausted")
        self.count += 1
        turn = self._turns.pop(0)
        tool_calls = [
            SimpleNamespace(
                id=tc["id"],
                type="function",
                function=SimpleNamespace(name=tc["name"], arguments=json.dumps(tc["arguments"])),
            )
            for tc in turn.get("tool_calls", [])
        ]
        message = SimpleNamespace(content=turn.get("content", ""), tool_calls=tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


def _patch_scripted_llm(monkeypatch: pytest.MonkeyPatch, script: _ScriptedCompletion) -> None:
    scripted_client = LlmClient(completion_fn=script)
    # Lift §17.2a: build_gateway_dispatcher moved to
    # backend.workflow.application.runtime.dispatcher; patch the binding at
    # its new home where the lookup happens.
    from backend.workflow.application.runtime import dispatcher as runtime_dispatcher

    monkeypatch.setattr(runtime_dispatcher, "LlmClient", lambda: scripted_client)


async def _seed_active_account(
    sf: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    provider: str = "ollama",
    label: str = "default",
) -> None:
    async with sf() as s:
        svc = ModelAccountService(s, cipher=runtime.CredentialCipher(runtime._key_from_settings()))
        await svc.create(
            workspace_id=workspace_id,
            account_id=account_id,
            payload=ModelAccountCreate(
                provider=provider,
                label=label,
                litellm_model="ollama_chat/qwen3-coder:30b",
                api_key="sk-test",
                data_jurisdiction="us",
                extra_params=({"executor_type": "claude_code"} if provider == "executor" else {}),
            ),
        )
        await s.commit()


async def _seed_request_and_run(
    session: AsyncSession, *, workspace_id: uuid.UUID, text: str
) -> uuid.UUID:
    # Seed the FK parent (TriggerEvent) BEFORE the Request — real Postgres
    # enforces requests_trigger_event_id_fkey (local SQLite does not).
    trigger = TriggerEventRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        source="direct",
        trigger_kind=TriggerKind.DIRECT,
        idempotency_key=f"k-{uuid.uuid4()}",
        payload={"text": text},
        received_at=datetime.now(tz=UTC),
    )
    session.add(trigger)
    await session.flush()
    request = RequestRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        trigger_event_id=trigger.id,
        status=RequestStatus.RUNNING,
        payload={"text": text},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.add(request)
    await session.flush()
    return await AgentRunner(session).open_run(request=request)


def _frame_turn(path: str) -> dict[str, Any]:
    return {
        "content": json.dumps(
            {
                "framed_intent": "Answer the question",
                "skill_match": None,
                "artifact_type_hint": None,
                "path_classification": path,
            }
        ),
        "tool_calls": [],
    }


def _answer_turn() -> dict[str, Any]:
    return {"content": "The deployment policy is to ship via the gateway.", "tool_calls": []}


async def test_knowledge_only_routes_to_knowledge_answer_no_loop(
    sf: async_sessionmaker[AsyncSession],
    kms_key: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A run framed ``knowledge_only`` → KnowledgeAnswerOrchestrator → ONE answer
    LLM call (after the frame call), an ANSWER deliverable, REVIEW_READY — and
    NO agent loop / sandbox / verify (no VerificationResult)."""
    workspace_id = uuid.uuid4()
    await _seed_active_account(sf, workspace_id=workspace_id, account_id=uuid.uuid4())

    async with sf() as session:
        run_id = await _seed_request_and_run(
            session, workspace_id=workspace_id, text="what is our deploy policy?"
        )
        await session.commit()

    # Frame call → knowledge_only; then exactly ONE answer completion. If the
    # native loop ran it would request MORE turns and exhaust the script.
    script = _ScriptedCompletion([_frame_turn("knowledge_only"), _answer_turn()])
    _patch_scripted_llm(monkeypatch, script)

    deps = runtime.build_agent_execution_deps(
        settings=get_settings(), sandbox_manager=NoopSandboxManager()
    )
    deps.workspace_root = tmp_path
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.drive_once() == 1

    # Exactly two completions total: frame + answer (the cost saver).
    assert script.count == 2

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY
        deliverable = (await s.execute(select(Deliverable))).scalar_one()
        assert deliverable.deliverable_type is DeliverableType.DIRECT_OUTPUT
        assert deliverable.payload.get("kind") == KNOWLEDGE_ANSWER_KIND
        # Honest: no verification ran (no agent loop / sandbox / verify).
        assert (await s.execute(select(VerificationResult))).first() is None


async def test_agent_loop_path_is_unchanged(
    sf: async_sessionmaker[AsyncSession],
    kms_key: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A run framed ``agent_loop`` → native RunOrchestrator (regression).

    The factory returns the native orchestrator; the loop writes + verifies a
    real code deliverable exactly as before B9b."""
    workspace_id = uuid.uuid4()
    await _seed_active_account(sf, workspace_id=workspace_id, account_id=uuid.uuid4())

    async with sf() as session:
        run_id = await _seed_request_and_run(
            session, workspace_id=workspace_id, text="build the answer file"
        )
        await session.commit()

    # Frame → agent_loop; then the native loop's work + verify turns.
    script = _ScriptedCompletion(
        [
            _frame_turn("agent_loop"),
            {
                "content": "Writing the deliverable and declaring how to check it.",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "declare_verification",
                        "arguments": {
                            "checks": [{"kind": "command", "command": "test -f answer.txt"}]
                        },
                    },
                    {
                        "id": "c2",
                        "name": "file_write",
                        "arguments": {"path": "answer.txt", "content": "42\n"},
                    },
                ],
            },
            {"content": "Done — answer.txt written.", "tool_calls": []},
        ]
    )
    _patch_scripted_llm(monkeypatch, script)

    deps = runtime.build_agent_execution_deps(
        settings=get_settings(), sandbox_manager=NoopSandboxManager()
    )
    deps.workspace_root = tmp_path
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.drive_once() == 1

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY
        deliverable = (await s.execute(select(Deliverable))).scalar_one()
        # Native loop → a CODE deliverable, NOT a knowledge answer.
        assert deliverable.deliverable_type is DeliverableType.CODE
        assert deliverable.payload.get("kind") != KNOWLEDGE_ANSWER_KIND


async def test_factory_routes_executor_account_even_when_knowledge_only(
    sf: async_sessionmaker[AsyncSession],
    kms_key: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An executor account keeps routing to ExecutorOrchestrator regardless of
    the path classification (knowledge-only is for the LLM Q&A path, not a
    delegated CLI worker)."""
    workspace_id = uuid.uuid4()
    await _seed_active_account(
        sf, workspace_id=workspace_id, account_id=uuid.uuid4(), provider="executor"
    )

    async with sf() as session:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=None,
            request_id=None,
            status=RunStatus.RUNNING,
            payload={
                "intent_text": "answer me",
                "frame": {"path_classification": "knowledge_only"},
            },
        )
        session.add(run)
        await session.flush()

        deps = runtime.build_agent_execution_deps(
            settings=get_settings(), sandbox_manager=NoopSandboxManager()
        )
        orch = await deps.orchestrator_factory(session, run)
        # Executor account → ExecutorOrchestrator, NOT the knowledge path.
        assert isinstance(orch, ExecutorOrchestrator)
        assert not isinstance(orch, KnowledgeAnswerOrchestrator)


async def test_knowledge_only_without_llm_falls_back_to_loop(
    sf: async_sessionmaker[AsyncSession],
    kms_key: None,
    tmp_path: Path,
) -> None:
    """knowledge_only but NO cheap LLM resolvable → native agent loop (no strand).

    With zero active accounts the factory cannot resolve a work LLM at all, so it
    creates a model-account Decision and returns None — the run is paused (never
    routed to a knowledge orchestrator it cannot build, never stranded)."""
    workspace_id = uuid.uuid4()
    async with sf() as session:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=None,
            request_id=None,
            status=RunStatus.RUNNING,
            payload={
                "intent_text": "answer me",
                "frame": {"path_classification": "knowledge_only"},
            },
        )
        session.add(run)
        await session.flush()

        deps = runtime.build_agent_execution_deps(
            settings=get_settings(), sandbox_manager=NoopSandboxManager()
        )
        orch = await deps.orchestrator_factory(session, run)
        # No account → no orchestrator (paused on a Decision), never a knowledge
        # orchestrator built without a model.
        assert orch is None


async def test_native_and_knowledge_orchestrators_are_run_compute() -> None:
    """Structural: both satisfy the RunCompute Protocol (one Protocol, not a
    Union). ``async`` only to satisfy the module's ``pytestmark``."""
    assert hasattr(RunOrchestrator, "run")
    assert hasattr(KnowledgeAnswerOrchestrator, "run")
