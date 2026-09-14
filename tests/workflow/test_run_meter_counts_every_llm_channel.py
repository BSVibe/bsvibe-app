"""#930 — the run meter must count EVERY LLM channel a run burns, not just act.

Measured premise (prod run ``08547545``): the payload carries ``"frame": {...}``
so the frame stage definitely made an LLM call, yet ``execution_runs.usage_*``
is ``14832 / 840`` — EXACTLY the usage of the single agentic ``executor_tasks``
row. A call happened whose tokens are absent from the total.

Channels that burned tokens no meter ever saw:

* the ``dispatcher`` seam (``_ResolverFrameLlm`` / ``_ResolverCompileLlm``)
  ended in ``return str(response.content)`` — the usage was ON the response and
  died on that line;
* :class:`VerificationService` reads ``turn.content`` from four LLM calls and
  never the usage riding alongside it;
* the product-tick planner's turn, same shape.

Each test pins the repair at the boundary that channel actually crosses. The
last one is the guard that matters: a run exercising act + frame + judge must
total STRICTLY MORE than its act turns alone. Today it totals exactly the act
turns, and that equality IS the bug.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from backend.dispatch.adapter import ChatResponse
from backend.extensions.skill.loader import SkillLoader
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.application.runtime.dispatcher import (
    UNATTRIBUTED_USAGE_EVENT,
    _ResolverCompileLlm,
    _ResolverFrameLlm,
)
from backend.workflow.application.stages.frame import (
    FrameConfig,
    FrameStage,
    TextCompletion,
)
from backend.workflow.application.verification_service import VerificationService
from backend.workflow.domain.verifier_contract import (
    VerificationCheck,
    VerificationContract,
)
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    RunAttempt,
    RunAttemptPhase,
    RunStatus,
    WorkStep,
    WorkStepStatus,
)
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.sandbox.protocol import SandboxResult
from backend.workflow.infrastructure.workers.agent_worker import AgentExecutionDeps, AgentWorker

from .._support import db_engine, memory_session

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------

_FRAME_JSON = json.dumps(
    {
        "framed_intent": "build the thing",
        "skill_match": None,
        "artifact_type_hint": "code",
        "path_classification": "agent_loop",
    }
)

_JUDGE_MARKER = "strict verification judge"


class _UsageAdapter:
    """A :class:`ModelAccountAdapter` whose every reply carries usage."""

    def __init__(self, *, content: str, prompt: int, completion: int) -> None:
        self._content = content
        self._prompt = prompt
        self._completion = completion
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        self.calls.append({"system": system, "messages": messages})
        return ChatResponse(
            content=self._content,
            usage_prompt_tokens=self._prompt,
            usage_completion_tokens=self._completion,
        )


class _UsageFrameLlm:
    """A :class:`FrameLlm` that burns a fixed, known number of tokens."""

    def __init__(self, *, prompt: int, completion: int, body: str = _FRAME_JSON) -> None:
        self.prompt = prompt
        self.completion = completion
        self._body = body
        self.calls = 0

    async def complete_text(self, *, system: str, user: str) -> TextCompletion:
        self.calls += 1
        return TextCompletion(
            text=self._body,
            usage_prompt_tokens=self.prompt,
            usage_completion_tokens=self.completion,
        )


class _MeteredLlm:
    """One object serving BOTH the act turns and the verify turns — exactly as
    production does (``RunOrchestrator._verifier`` hands the service its own
    ``self._llm``).

    It classifies each call by the prompt it was handed so the test can state
    "the act turns alone" as a number, which is what the regression guard
    compares against.
    """

    ACT_PROMPT, ACT_COMPLETION = 7, 3
    VERIFY_PROMPT, VERIFY_COMPLETION = 50, 20

    def __init__(self, act_turns: list[LoopTurn]) -> None:
        self._act_turns = list(act_turns)
        self.act_prompt = 0
        self.act_completion = 0
        self.verify_prompt = 0
        self.verify_completion = 0
        self.verify_calls = 0
        self.judge_calls = 0

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        blob = "\n".join(str(m.get("content") or "") for m in messages if isinstance(m, dict))
        is_verify = (
            _JUDGE_MARKER in blob
            or "verifier" in blob
            or "scope reviewer" in blob
            or "verification commands" in blob
        )
        if _JUDGE_MARKER in blob:
            self.judge_calls += 1
        if not is_verify and self._act_turns:
            turn = self._act_turns.pop(0)
            self.act_prompt += self.ACT_PROMPT
            self.act_completion += self.ACT_COMPLETION
            return LoopTurn(
                content=turn.content,
                tool_calls=turn.tool_calls,
                usage_prompt_tokens=self.ACT_PROMPT,
                usage_completion_tokens=self.ACT_COMPLETION,
            )
        self.verify_calls += 1
        self.verify_prompt += self.VERIFY_PROMPT
        self.verify_completion += self.VERIFY_COMPLETION
        return LoopTurn(
            content=json.dumps({"passed": True, "reasoning": "ok"}),
            tool_calls=(),
            usage_prompt_tokens=self.VERIFY_PROMPT,
            usage_completion_tokens=self.VERIFY_COMPLETION,
        )


class _StubJudgeLlm:
    """A :class:`JudgeLlm` whose every turn reports a fixed usage."""

    def __init__(self, *, prompt: int, completion: int, content: str | None = None) -> None:
        self.prompt = prompt
        self.completion = completion
        self._content = content or json.dumps({"passed": True, "reasoning": "ok"})
        self.calls = 0

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        self.calls += 1
        return LoopTurn(
            content=self._content,
            tool_calls=(),
            usage_prompt_tokens=self.prompt,
            usage_completion_tokens=self.completion,
        )


class _Box:
    """A sandbox session whose commands pass and whose files are scriptable."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self._files = files or {}

    @property
    def workspace_mount(self) -> str:
        return "/workspace"

    async def exec(self, command: str, *, timeout_s: float = 0.0, **kw: Any) -> SandboxResult:
        return SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        return self._files.get(rel_path, b"")

    async def write_file(self, rel_path: str, content: bytes) -> None:
        self._files[rel_path] = content

    async def list_dir(self, rel_path: str) -> list[str]:
        return list(self._files)


def _tc(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"c-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


async def _seed_run(session: AsyncSession, *, intent: str = "do the thing") -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=None,
        status=RunStatus.RUNNING,
        payload={"intent_text": intent},
    )
    session.add(run)
    await session.flush()
    return run


async def _seed_step_and_attempt(
    session: AsyncSession, run: ExecutionRun
) -> tuple[WorkStep, RunAttempt]:
    step = WorkStep(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=run.workspace_id,
        title="t",
        status=WorkStepStatus.RUNNING,
        payload={},
    )
    attempt = RunAttempt(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=run.workspace_id,
        phase=RunAttemptPhase.VERIFYING,
        payload={},
    )
    session.add_all([step, attempt])
    await session.flush()
    return step, attempt


async def _seed_request_and_run(
    session: AsyncSession, *, workspace_id: uuid.UUID, text: str
) -> uuid.UUID:
    # Seed the FK parent (TriggerEvent) BEFORE the Request — real Postgres
    # enforces ``requests_trigger_event_id_fkey``; local SQLite does not.
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


def _skill_loader_factory(root: Path):
    def _skill_loader_for(ws_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(root / str(ws_id))
        loader.load_all()
        return loader

    return _skill_loader_for


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


# --------------------------------------------------------------------------
# 1. The seam returns the usage instead of dropping it
# --------------------------------------------------------------------------


async def test_frame_seam_hands_back_the_usage_the_adapter_reported() -> None:
    """``_ResolverFrameLlm.complete_text`` used to ``return str(response.content)``
    — the usage rode in on the response and died on that line."""
    adapter = _UsageAdapter(content="hello", prompt=131, completion=17)
    seam = _ResolverFrameLlm(adapter=adapter)

    completion = await seam.complete_text(system="s", user="u")

    assert completion.text == "hello"
    assert completion.usage_prompt_tokens == 131
    assert completion.usage_completion_tokens == 17


async def test_frame_stage_carries_the_framing_turns_usage_out(tmp_path: Path) -> None:
    """The stage is the only thing between the seam and the run — if it drops
    the number the seam now returns, nothing downstream can accrue it."""
    llm = _UsageFrameLlm(prompt=211, completion=29)
    loader = SkillLoader(tmp_path / "skills")
    loader.load_all()
    request = RequestRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        trigger_event_id=uuid.uuid4(),
        status=RequestStatus.RUNNING,
        payload={"text": "build the thing"},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    framed = await FrameStage().frame(
        request=request, config=FrameConfig(skill_loader=loader, llm=llm)
    )

    assert llm.calls == 1
    assert framed.usage_prompt_tokens == 211
    assert framed.usage_completion_tokens == 29


# --------------------------------------------------------------------------
# 2. verify / judge accrual — more than one of the four call sites
# --------------------------------------------------------------------------


async def test_verify_accrues_every_llm_turn_it_burns_onto_the_run() -> None:
    """``verify()`` drives the demonstration planner AND the judge through
    ``self._llm``; both usages must land on the run's meter."""
    llm = _StubJudgeLlm(prompt=40, completion=9)
    async with memory_session() as session:
        run = await _seed_run(session)
        step, attempt = await _seed_step_and_attempt(session, run)
        svc = VerificationService(session=session, llm=llm)
        contract = VerificationContract(
            checks=(VerificationCheck(kind="judge", criteria=("it works",)),)
        )

        await svc.verify(
            run=run,
            work_step=step,
            attempt=attempt,
            contract=contract,
            box=_Box(files={"answer.py": b"def f():\n    return 1\n"}),
            written_paths=["answer.py"],
            final_text="done",
        )

        assert llm.calls >= 2, "expected at least the planner turn and the judge turn"
        assert run.usage_prompt_tokens == llm.calls * 40
        assert run.usage_completion_tokens == llm.calls * 9


async def test_scope_judge_turn_accrues_onto_the_run() -> None:
    """Call site ``:1414`` — the scope judge. Gated to a product run on a real
    worktree, so the run's worktree gets a ``.git`` marker."""
    from backend.storage.product_workspace import run_worktree_path

    llm = _StubJudgeLlm(
        prompt=13, completion=5, content=json.dumps({"verdict": "clean", "flagged": []})
    )
    async with memory_session() as session:
        run = await _seed_run(session)
        run.product_id = uuid.uuid4()
        worktree = run_worktree_path(run.id)
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")

        svc = VerificationService(session=session, llm=llm)
        out = await svc._run_scope_check(run, ["backend/a.py"])

        assert out is not None, "the scope judge must have RUN for this to measure anything"
        assert llm.calls == 1
        assert run.usage_prompt_tokens == 13
        assert run.usage_completion_tokens == 5


async def test_gate_deriver_turn_accrues_onto_the_run() -> None:
    """Call site ``:1080`` — the derived-gate author. #926 investigated this
    exact call; even had the worker's report landed, its tokens were invisible."""
    llm = _StubJudgeLlm(
        prompt=77,
        completion=11,
        content=json.dumps({"applicable": False, "commands": []}),
    )
    async with memory_session() as session:
        run = await _seed_run(session)
        svc = VerificationService(session=session, llm=llm)

        await svc._author_derived_gate(
            run,
            "do the thing",
            {"pyproject.toml": "[project]"},
            ["backend/a.py"],
        )

        assert llm.calls == 1
        assert run.usage_prompt_tokens == 77
        assert run.usage_completion_tokens == 11


# --------------------------------------------------------------------------
# 3. The frame→run seam — it must CROSS the boundary, not call each half
# --------------------------------------------------------------------------


async def test_frame_usage_lands_on_the_run_across_the_worker_boundary(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The frame stage takes a ``RequestRow``; the run is a different object
    reached in a different method. A gap of exactly this shape survived in this
    repo because every test called the two halves directly (PR #945) — so this
    drives the REAL worker and reads the REAL run row back out of the DB."""
    workspace_id = uuid.uuid4()
    async with sf() as session:
        run_id = await _seed_request_and_run(
            session, workspace_id=workspace_id, text="build the thing"
        )
        await session.commit()

    frame_llm = _UsageFrameLlm(prompt=303, completion=41)
    skill_loader_for = _skill_loader_factory(tmp_path / "skills")

    def _orchestrator_factory(session: AsyncSession, run: ExecutionRun) -> RunOrchestrator:
        return RunOrchestrator(
            session=session,
            llm=_MeteredLlm([LoopTurn(content="nothing to do", tool_calls=())]),
            sandbox_manager=NoopSandboxManager(),
            skill_loader=skill_loader_for(run.workspace_id),
        )

    agent = AgentWorker(
        session_factory=sf,
        execution=AgentExecutionDeps(
            skill_loader_for=skill_loader_for,
            orchestrator_factory=_orchestrator_factory,
            workspace_root=tmp_path / "runs",
            frame_llm=frame_llm,
        ),
    )
    assert await agent.drive_once() == 1
    assert frame_llm.calls == 1

    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert "frame" in (run.payload or {}), "framing must actually have happened"
        assert run.usage_prompt_tokens >= 303
        assert run.usage_completion_tokens >= 41


# --------------------------------------------------------------------------
# 4. The regression guard that matters
# --------------------------------------------------------------------------


async def test_a_run_totals_more_than_its_act_turns_alone(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """act + frame + judge. Today the run's meter equals the act turns EXACTLY
    — that equality is the bug (prod ``08547545``), and this is what catches
    its return."""
    workspace_id = uuid.uuid4()
    async with sf() as session:
        run_id = await _seed_request_and_run(
            session, workspace_id=workspace_id, text="build the thing"
        )
        await session.commit()

    frame_llm = _UsageFrameLlm(prompt=100, completion=10)
    metered = _MeteredLlm(
        [
            LoopTurn(
                content="declaring and writing",
                tool_calls=(
                    _tc("declare_verification", checks=[{"kind": "judge", "criteria": ["works"]}]),
                    _tc("file_write", path="answer.txt", content="42\n"),
                ),
            ),
            LoopTurn(content="Done.", tool_calls=()),
        ]
    )
    skill_loader_for = _skill_loader_factory(tmp_path / "skills")

    def _orchestrator_factory(session: AsyncSession, run: ExecutionRun) -> RunOrchestrator:
        return RunOrchestrator(
            session=session,
            llm=metered,
            sandbox_manager=NoopSandboxManager(),
            skill_loader=skill_loader_for(run.workspace_id),
        )

    agent = AgentWorker(
        session_factory=sf,
        execution=AgentExecutionDeps(
            skill_loader_for=skill_loader_for,
            orchestrator_factory=_orchestrator_factory,
            workspace_root=tmp_path / "runs",
            frame_llm=frame_llm,
        ),
    )
    assert await agent.drive_once() == 1

    act_total = metered.act_prompt + metered.act_completion
    frame_total = frame_llm.prompt + frame_llm.completion
    verify_total = metered.verify_prompt + metered.verify_completion

    # Companion assertions — the comparison below is meaningless if any of the
    # three channels never ran.
    assert act_total > 0, "no act turn ran — the guard would pass vacuously"
    assert frame_llm.calls == 1, "no frame turn ran"
    assert metered.judge_calls >= 1, "no judge turn ran"

    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        total = run.usage_prompt_tokens + run.usage_completion_tokens
        assert total > act_total, (
            f"the run meter counted only the act turns ({act_total}); "
            f"frame burned {frame_total} and verify burned {verify_total}"
        )
        assert total == act_total + frame_total + verify_total


# --------------------------------------------------------------------------
# 5. No run in scope → a NAMED log event, never an invented home
# --------------------------------------------------------------------------


async def test_compile_seam_logs_its_usage_as_unattributed() -> None:
    """Knowledge ingest / product bootstrap / the settle extractor have no run
    to accrue to. Do not attach them to an unrelated run and do not silently
    drop them — log the number with enough identity to attribute it later."""
    workspace_id = uuid.uuid4()
    adapter = _UsageAdapter(content="[]", prompt=900, completion=80)
    seam = _ResolverCompileLlm(adapter=adapter, workspace_id=workspace_id, site="knowledge.ingest")

    with capture_logs() as logs:
        text = await seam.chat(system="s", messages=[{"role": "user", "content": "u"}])

    assert text == "[]"
    events = [e for e in logs if e.get("event") == UNATTRIBUTED_USAGE_EVENT]
    assert events, f"expected a {UNATTRIBUTED_USAGE_EVENT} event, got {logs}"
    event = events[0]
    assert event["usage_prompt_tokens"] == 900
    assert event["usage_completion_tokens"] == 80
    assert event["workspace_id"] == str(workspace_id)
    assert event["site"] == "knowledge.ingest"


async def test_concept_framer_logs_its_usage_as_unattributed() -> None:
    """The settle-knowledge concept framer runs on ``_ResolverFrameLlm`` too,
    but its caller (the promoter) has no run — so it takes the SAME named
    event rather than an invented home."""
    from backend.workflow.application.runtime.settle_runtime import _RoutedConceptFramer

    workspace_id = uuid.uuid4()
    adapter = _UsageAdapter(content="a synthesis.", prompt=55, completion=6)
    framer = _RoutedConceptFramer(_ResolverFrameLlm(adapter=adapter), workspace_id=workspace_id)

    with capture_logs() as logs:
        out = await framer.frame(concept="x", members=[("a", "a detail")])

    assert out == "a synthesis."
    events = [e for e in logs if e.get("event") == UNATTRIBUTED_USAGE_EVENT]
    assert events, f"expected a {UNATTRIBUTED_USAGE_EVENT} event, got {logs}"
    assert events[0]["usage_prompt_tokens"] == 55
    assert events[0]["workspace_id"] == str(workspace_id)


# --------------------------------------------------------------------------
# 6. The product-tick planner — a FOURTH channel, found while fixing the three
# --------------------------------------------------------------------------


async def test_tick_planner_carries_its_turns_usage_out() -> None:
    """``ProductTickPlanner`` runs on a ``ResolverLoopLlm`` (usage IS on the
    turn) inside an already-open run, and nothing read it — the same leak shape
    as frame and judge, found while auditing the seam."""
    from backend.workflow.application.product_tick_planner import _parse_plan

    plan = _parse_plan(
        json.dumps({"instruction": "do X", "rationale": "because"}),
        usage_prompt_tokens=64,
        usage_completion_tokens=12,
    )

    assert plan is not None
    assert plan.usage_prompt_tokens == 64
    assert plan.usage_completion_tokens == 12


async def test_tick_planner_usage_lands_on_the_run(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Seam test for the tick channel: the planner turn's tokens must be on the
    run row the worker just planned for."""
    from backend.workflow.application.product_tick_planner import TickPlan

    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()

    class _StubPlanner:
        def __init__(self) -> None:
            self.calls = 0

        async def plan(self, *, workspace_id: uuid.UUID, product_id: uuid.UUID) -> TickPlan:
            self.calls += 1
            return TickPlan(
                instruction="Add the signature check",
                rationale="prior run left it open",
                usage_prompt_tokens=880,
                usage_completion_tokens=44,
            )

    async with sf() as session:
        trigger = TriggerEventRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=product_id,
            source="schedule",
            trigger_kind=TriggerKind.SCHEDULE,
            idempotency_key=f"k-{uuid.uuid4()}",
            payload={"text": "advance the product"},
            received_at=datetime.now(tz=UTC),
        )
        session.add(trigger)
        await session.flush()
        request = RequestRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            trigger_event_id=trigger.id,
            product_id=product_id,
            status=RequestStatus.RUNNING,
            payload={"text": "advance the product", "kind": "product_tick"},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        session.add(request)
        await session.flush()
        run_id = await AgentRunner(session).open_run(request=request)
        await session.commit()

    planner = _StubPlanner()
    # Zero-usage framing so the assertion isolates the PLANNER's tokens.
    frame_llm = _UsageFrameLlm(prompt=0, completion=0)
    skill_loader_for = _skill_loader_factory(tmp_path / "skills")

    def _orchestrator_factory(session: AsyncSession, run: ExecutionRun) -> None:
        # None pauses the run right after framing — enough to read the meter.
        return None

    agent = AgentWorker(
        session_factory=sf,
        execution=AgentExecutionDeps(
            skill_loader_for=skill_loader_for,
            orchestrator_factory=_orchestrator_factory,
            workspace_root=tmp_path / "runs",
            frame_llm=frame_llm,
            tick_planner_for=lambda session: planner,
        ),
    )
    assert await agent.drive_once() == 1
    assert planner.calls == 1, "the planner must have RUN for this to measure anything"

    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert run.usage_prompt_tokens == 880
        assert run.usage_completion_tokens == 44
