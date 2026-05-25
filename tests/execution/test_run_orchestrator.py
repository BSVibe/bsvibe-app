"""RunOrchestrator compute-loop tests (execution layer, no HTTP).

A deterministic stub LLM drives the loop; a real host-side
``NoopSandboxManager`` does the file work + runs the verify command
checks. These prove the §11.3 plan → act → verify → iterate loop end to
end without touching a real model or Docker.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.db import ConnectorAccountRow
from backend.delivery.db import DeliveryEventRow
from backend.execution.connector_actions import ConnectorActionTool
from backend.execution.db import (
    Decision,
    Deliverable,
    ExecutionRun,
    ExecutionRunActivity,
    ProofState,
    RunAttempt,
    RunAttemptPhase,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.orchestrator import (
    CanonRetriever,
    LoopLlm,
    LoopToolCall,
    LoopTurn,
    RunOrchestrator,
)
from backend.plugins.base import ActionCapability, PluginMeta
from backend.plugins.context import SkillContext
from backend.skills.loader import SkillLoader
from backend.skills.tool_binding import INVOKE_SKILL_NAME
from backend.supervisor.sandbox import NoopSandboxManager, SandboxUnavailable
from backend.workspaces.db import ProductRow, WorkspaceRow
from tests._support import memory_session

# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class ScriptedLlm:
    """A deterministic :class:`LoopLlm` — pops the next pre-programmed
    turn on each ``complete`` call (FIFO). Records the (messages, tools)
    each call saw for assertions. Raises if the loop asks for more turns
    than scripted (catches runaway loops)."""

    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._turns:
            raise AssertionError("ScriptedLlm exhausted — loop requested an unscripted turn")
        return self._turns.pop(0)


class FailingSandboxManager:
    """A sandbox manager whose acquire blows up — simulates infra failure."""

    async def acquire(self, project_id: uuid.UUID, workspace_path: str) -> Any:
        raise SandboxUnavailable("docker daemon unreachable")

    async def release(self, project_id: uuid.UUID) -> None:
        return None


class StubRetriever:
    """A :class:`CanonRetriever` returning fixed canonical patterns."""

    def __init__(self, patterns: list[str]) -> None:
        self._patterns = patterns
        self.queried: list[str] = []

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        self.queried.append(signals)
        return list(self._patterns)


def _write_skill(skill_dir: Path, name: str, body: str, description: str = "desc") -> None:
    """Author a workspace skill manifest the SkillLoader can parse."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / f"{name}.md").write_text(
        f"---\nname: {name}\nversion: 1\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )


def _tc(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


def _tc_args(name: str, arguments: dict[str, Any]) -> LoopToolCall:
    """Build a tool call whose arguments dict may use keys that collide with
    :func:`_tc`'s own params (e.g. an ``invoke_skill`` call's ``name`` arg)."""
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


def _declare_command(command: str) -> LoopToolCall:
    return _tc("declare_verification", checks=[{"kind": "command", "command": command}])


def _declare_judge(*criteria: str) -> LoopToolCall:
    return _tc("declare_verification", checks=[{"kind": "judge", "criteria": list(criteria)}])


async def _make_run(
    session: AsyncSession,
    *,
    intent: str = "do the thing",
    product_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id or uuid.uuid4(),
        product_id=product_id,
        request_id=None,
        status=RunStatus.RUNNING,
        payload={"intent_text": intent},
    )
    session.add(run)
    await session.flush()
    return run


async def _make_product(
    session: AsyncSession, *, workspace_id: uuid.UUID, slug: str, name: str
) -> ProductRow:
    """Seed a workspace + product so the orchestrator can resolve the run's
    product binding for the settle payload."""
    session.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1", safe_mode=True))
    product = ProductRow(id=uuid.uuid4(), workspace_id=workspace_id, name=name, slug=slug)
    session.add(product)
    await session.flush()
    return product


# --------------------------------------------------------------------------
# verified path — real file work + a command check that passes
# --------------------------------------------------------------------------


async def test_verified_run_does_file_work_and_passes_command_check(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="I'll write the answer and declare a check.",
                tool_calls=(
                    _declare_command("grep -q 42 answer.txt"),
                    _tc("file_write", path="answer.txt", content="42\n"),
                ),
            ),
            LoopTurn(content="Done — the answer is written.", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        assert result.written_paths == ["answer.txt"]
        # Real file landed on disk.
        assert (tmp_path / "answer.txt").read_text() == "42\n"

        work_step = (await session.execute(select(WorkStep))).scalar_one()
        assert work_step.status is WorkStepStatus.VERIFIED
        assert work_step.proof_state is ProofState.PROVED

        attempt = (await session.execute(select(RunAttempt))).scalar_one()
        assert attempt.phase is RunAttemptPhase.COMPLETED

        vr = (await session.execute(select(VerificationResult))).scalar_one()
        assert vr.outcome is VerificationOutcome.PASSED

        deliverable = (await session.execute(select(Deliverable))).scalar_one()
        assert "answer.txt" in (deliverable.payload.get("artifact_refs") or [])

        # A Deliver event was emitted into the table the DeliveryWorker drains.
        deliver_event = (await session.execute(select(DeliveryEventRow))).scalar_one()
        assert deliver_event.deliverable_id == deliverable.id

        # Settle observation recorded as run activity.
        activities = (await session.execute(select(ExecutionRunActivity))).scalars().all()
        assert any(a.activity_type == "settle" for a in activities)


def _settle_payload(activities) -> dict:
    settle = next(a for a in activities if a.activity_type == "settle")
    return settle.payload


async def test_settle_payload_carries_product_and_intent(tmp_path: Path) -> None:
    """The settle activity must thread the run's STABLE context — product
    binding (slug/name resolved from product_id) + founder intent_text — so the
    SettleWorker can cluster garden observations by product + intent."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f marker"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    ws = uuid.uuid4()
    async with memory_session() as session:
        product = await _make_product(
            session, workspace_id=ws, slug="vaultwarden-selfhost", name="Vaultwarden Self-Host"
        )
        run = await _make_run(
            session,
            intent="Set up the vaultwarden password manager on the mini",
            product_id=product.id,
            workspace_id=ws,
        )
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

        activities = (await session.execute(select(ExecutionRunActivity))).scalars().all()
        payload = _settle_payload(activities)
        assert payload["product_slug"] == "vaultwarden-selfhost"
        assert payload["product_name"] == "Vaultwarden Self-Host"
        assert payload["intent_text"] == "Set up the vaultwarden password manager on the mini"
        # The PR #27 fields are still present (additive).
        assert payload["verified"] is True
        assert payload["artifact_refs"] == ["marker"]
        assert "summary" in payload


async def test_settle_payload_degrades_without_product(tmp_path: Path) -> None:
    """A run with no product binding (connector-inbound) omits product keys but
    still carries intent_text — graceful degradation, no synthetic blanks."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f marker"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session, intent="harden the cache")
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

        activities = (await session.execute(select(ExecutionRunActivity))).scalars().all()
        payload = _settle_payload(activities)
        assert "product_slug" not in payload
        assert "product_name" not in payload
        assert payload["intent_text"] == "harden the cache"


async def test_verified_run_no_extra_llm_calls(tmp_path: Path) -> None:
    """A command-only contract needs no judge call — the loop must not
    make an unscripted LLM round."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f marker"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
    assert result.outcome == "verified"
    assert len(llm.calls) == 2  # two plan turns, zero judge calls


# --------------------------------------------------------------------------
# judge path — non-executable criteria graded by the LLM
# --------------------------------------------------------------------------


async def test_verified_via_judge_check(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_judge("the file greets the world"),
                    _tc("file_write", path="hello.txt", content="Hello, world"),
                ),
            ),
            LoopTurn(content="written the greeting", tool_calls=()),
            # judge call (tools=None) returns a pass verdict as JSON
            LoopTurn(content='{"passed": true, "reasoning": "greets the world"}', tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

    assert result.outcome == "verified"
    # The judge call was made with no tools.
    assert llm.calls[-1]["tools"] is None


async def test_judge_fail_then_decision_at_cap(tmp_path: Path) -> None:
    """A judge verdict of fail with the cycle cap reached → needs_decision."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_judge("the file contains a valid proof"),
                    _tc("file_write", path="proof.txt", content="nope"),
                ),
            ),
            LoopTurn(content="attempted", tool_calls=()),
            LoopTurn(content='{"passed": false, "reasoning": "no proof present"}', tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager(), max_cycles=2
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

    assert result.outcome == "needs_decision"
    assert result.decision_id is not None


# --------------------------------------------------------------------------
# iterate: verify fails, re-plan, then pass
# --------------------------------------------------------------------------


async def test_failed_verify_then_replan_then_verified(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("grep -q DONE result.txt"),
                    _tc("file_write", path="result.txt", content="WIP"),
                ),
            ),
            LoopTurn(content="first pass", tool_calls=()),  # → verify FAIL
            LoopTurn(
                content="fixing", tool_calls=(_tc("file_write", path="result.txt", content="DONE"),)
            ),
            LoopTurn(content="second pass", tool_calls=()),  # → verify PASS
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager(), max_cycles=6
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        results = (await session.execute(select(VerificationResult))).scalars().all()
        outcomes = sorted(r.outcome.value for r in results)
        assert "failed" in outcomes
        assert "passed" in outcomes


# --------------------------------------------------------------------------
# needs_decision paths
# --------------------------------------------------------------------------


async def test_ask_user_question_creates_decision_and_pauses(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="I need the founder to decide.",
                tool_calls=(_tc("ask_user_question", question="Which database should I target?"),),
            ),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "needs_decision"
        decision = (await session.execute(select(Decision))).scalar_one()
        assert result.decision_id == decision.id
        assert "database" in (decision.payload.get("question") or "")
        # No verification ran.
        assert (await session.execute(select(VerificationResult))).first() is None
        # The work step did NOT reach a verified terminal.
        work_step = (await session.execute(select(WorkStep))).scalar_one()
        assert work_step.status is not WorkStepStatus.VERIFIED


async def test_no_contract_declared_routes_to_decision(tmp_path: Path) -> None:
    """Work that finishes without ever declaring a contract is never a
    silent pass — it becomes a human-review Decision."""
    llm = ScriptedLlm(
        [
            LoopTurn(content="", tool_calls=(_tc("file_write", path="foo.txt", content="bar"),)),
            LoopTurn(content="I think I'm done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "needs_decision"
        decision = (await session.execute(select(Decision))).scalar_one()
        assert decision.decision == "human_review_required"


# --------------------------------------------------------------------------
# system_error path
# --------------------------------------------------------------------------


class _RaisingLlm:
    """A LoopLlm that blows up mid-plan — simulates an in-loop crash."""

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        raise RuntimeError("model backend exploded")


async def test_loop_crash_yields_system_error(tmp_path: Path) -> None:
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session, llm=_RaisingLlm(), sandbox_manager=NoopSandboxManager()
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "system_error"
        attempt = (await session.execute(select(RunAttempt))).scalar_one()
        assert attempt.phase is RunAttemptPhase.FAILED
        # The crash was recorded as an activity, not leaked.
        activities = (await session.execute(select(ExecutionRunActivity))).scalars().all()
        assert any(a.activity_type == "error" for a in activities)


async def test_sandbox_failure_yields_system_error(tmp_path: Path) -> None:
    llm = ScriptedLlm([LoopTurn(content="never reached", tool_calls=())])
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=FailingSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "system_error"
        attempt = (await session.execute(select(RunAttempt))).scalar_one()
        assert attempt.phase is RunAttemptPhase.FAILED


# --------------------------------------------------------------------------
# BSage retrieval folds canonical patterns into the contract as judge criteria
# --------------------------------------------------------------------------


async def test_retriever_folds_canonical_patterns_into_contract(tmp_path: Path) -> None:
    retriever = StubRetriever(["always pin dependency versions"])
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f deps.txt"),
                    _tc("file_write", path="deps.txt", content="pkg==1.0"),
                ),
            ),
            LoopTurn(content="declared deps", tool_calls=()),
            # judge call for the folded canonical criterion
            LoopTurn(content='{"passed": true}', tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            retriever=retriever,
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        assert retriever.queried, "retriever should have been queried with the change signals"
        vr = (await session.execute(select(VerificationResult))).scalar_one()
        # The folded canonical pattern shows up as a judge criterion in the contract.
        contract_blob = str(vr.contract)
        assert "pin dependency versions" in contract_blob


def test_loop_protocols_are_runtime_checkable() -> None:
    assert isinstance(ScriptedLlm([]), LoopLlm)
    assert isinstance(StubRetriever([]), CanonRetriever)


# --------------------------------------------------------------------------
# B5a — invoke_skill + knowledge_search are now reachable by the loop
# --------------------------------------------------------------------------


def _tool_names(tools_schema: list[dict[str, Any]]) -> set[str]:
    return {t["function"]["name"] for t in tools_schema if t.get("type") == "function"}


async def test_loop_tool_schema_includes_skill_and_knowledge_tools(tmp_path: Path) -> None:
    """The schema surfaced to the work LLM now includes ``invoke_skill`` and
    ``knowledge_search`` when a skill loader is wired in (was absent before B5a).
    """
    llm = ScriptedLlm([LoopTurn(content="done", tool_calls=())])
    skill_dir = tmp_path / "skills"
    _write_skill(skill_dir, "weekly-digest", "Summarize the week.")
    loader = SkillLoader(skill_dir)
    loader.load_all()
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            skill_loader=loader,
            retriever=StubRetriever([]),
        )
        await orch.run(run=run, workspace_dir=tmp_path / "ws")

    # The first plan turn saw the surfaced tool schema.
    first_tools = llm.calls[0]["tools"]
    assert first_tools is not None
    names = _tool_names(first_tools)
    assert INVOKE_SKILL_NAME in names
    assert "knowledge_search" in names
    # Existing tools still present.
    assert "file_write" in names
    assert "declare_verification" in names
    assert "ask_user_question" in names


async def test_loop_invokes_workspace_skill(tmp_path: Path) -> None:
    """A stub LLM that emits an ``invoke_skill`` tool call → the skill runner
    runs (injecting the skill body as the system prompt) and the result is fed
    back to the loop. Proves ``register_invoke_skill`` is actually called with
    the workspace loader (it was never called before B5a)."""
    skill_dir = tmp_path / "skills"
    _write_skill(skill_dir, "weekly-digest", "SKILL-BODY-MARKER summarize the week.")
    loader = SkillLoader(skill_dir)
    loader.load_all()

    captured: dict[str, Any] = {}

    class RecordingLlm:
        """Records the system prompt the skill runner's completion seam sees,
        then drives the loop to a verified terminal."""

        def __init__(self) -> None:
            self._turns = [
                LoopTurn(
                    content="invoking the skill",
                    tool_calls=(
                        _tc_args("invoke_skill", {"name": "weekly-digest", "input": "go"}),
                    ),
                ),
                LoopTurn(
                    content="",
                    tool_calls=(
                        _declare_command("test -f out.txt"),
                        _tc("file_write", path="out.txt", content="x"),
                    ),
                ),
                LoopTurn(content="done", tool_calls=()),
            ]
            self.calls: list[dict[str, Any]] = []

        async def complete(
            self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
        ) -> LoopTurn:
            # The skill runner routes its completion through the SAME loop LLM
            # seam, but as a plain completion (no tools). Capture the system
            # prompt of that call so we can prove the skill body was injected.
            if tools is None and messages and messages[0].get("role") == "system":
                content = messages[0].get("content") or ""
                if "SKILL-BODY-MARKER" in content:
                    captured["skill_system_prompt"] = content
                    return LoopTurn(content="skill response text", tool_calls=())
            self.calls.append({"messages": list(messages), "tools": tools})
            if not self._turns:
                raise AssertionError("RecordingLlm exhausted")
            return self._turns.pop(0)

    llm = RecordingLlm()
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            skill_loader=loader,
            retriever=StubRetriever([]),
        )
        result = await orch.run(run=run, workspace_dir=tmp_path / "ws")

    assert result.outcome == "verified"
    # The skill runner actually ran with the workspace skill's body injected.
    assert "SKILL-BODY-MARKER" in captured.get("skill_system_prompt", "")
    # The skill result was fed back as a tool message the loop continued from.
    tool_msgs = [
        m
        for call in llm.calls
        for m in call["messages"]
        if m.get("role") == "tool" and "weekly-digest" in (m.get("content") or "")
    ]
    assert tool_msgs, "the invoke_skill result should be appended as a tool message"


async def test_knowledge_search_tool_returns_workspace_knowledge(tmp_path: Path) -> None:
    """A stub LLM that calls ``knowledge_search`` → the tool returns the
    workspace's knowledge for the query, fed back as a tool message."""
    retriever = StubRetriever(["always pin dependency versions"])
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="consulting knowledge",
                tool_calls=(_tc("knowledge_search", query="dependency"),),
            ),
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f out.txt"),
                    _tc("file_write", path="out.txt", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
            # A non-empty retriever folds its patterns into the verify contract
            # as a judge criterion (B3), so the verify step makes one judge call.
            LoopTurn(content='{"passed": true}', tool_calls=()),
        ]
    )
    skill_dir = tmp_path / "skills"
    _write_skill(skill_dir, "weekly-digest", "Summarize.")
    loader = SkillLoader(skill_dir)
    loader.load_all()
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            skill_loader=loader,
            retriever=retriever,
        )
        result = await orch.run(run=run, workspace_dir=tmp_path / "ws")

    assert result.outcome == "verified"
    assert retriever.queried, "knowledge_search should query the retriever"
    tool_msgs = [
        m
        for call in llm.calls
        for m in call["messages"]
        if m.get("role") == "tool" and "pin dependency versions" in (m.get("content") or "")
    ]
    assert tool_msgs, "the knowledge_search result should be appended as a tool message"


async def test_knowledge_search_empty_workspace_returns_valid_empty(tmp_path: Path) -> None:
    """An empty-knowledge workspace → knowledge_search returns an empty-but-valid
    result and never crashes the loop."""
    retriever = StubRetriever([])
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="consulting knowledge",
                tool_calls=(_tc("knowledge_search", query="anything"),),
            ),
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f out.txt"),
                    _tc("file_write", path="out.txt", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    skill_dir = tmp_path / "skills"
    _write_skill(skill_dir, "weekly-digest", "Summarize.")
    loader = SkillLoader(skill_dir)
    loader.load_all()
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            skill_loader=loader,
            retriever=retriever,
        )
        result = await orch.run(run=run, workspace_dir=tmp_path / "ws")

    assert result.outcome == "verified"


async def test_loop_without_skill_loader_omits_skill_tools(tmp_path: Path) -> None:
    """Backward-compat: a run with no skill loader wired (existing callers /
    tests) surfaces neither new tool — the loop is unchanged."""
    llm = ScriptedLlm([LoopTurn(content="done", tool_calls=())])
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        await orch.run(run=run, workspace_dir=tmp_path)

    first_tools = llm.calls[0]["tools"]
    assert first_tools is not None
    names = _tool_names(first_tools)
    assert INVOKE_SKILL_NAME not in names
    assert "knowledge_search" not in names
    assert "file_write" in names


# --------------------------------------------------------------------------
# B5b — connector @p.action surfaced as loop tools, gated by DangerAnalyzer
# --------------------------------------------------------------------------


def _fake_action_plugin(
    name: str,
    *,
    action_name: str,
    input_schema: dict[str, Any] | None = None,
    mcp_exposed: bool = True,
) -> PluginMeta:
    """A PluginMeta carrying a single ``@p.action``-style capability."""
    recorded: dict[str, Any] = {}

    async def _fn(context: Any, **kwargs: Any) -> dict[str, Any]:
        recorded["context"] = context
        recorded["kwargs"] = kwargs
        return {"ok": True, "echo": kwargs}

    meta = PluginMeta(
        name=name,
        version="0",
        description="fake connector",
        author="t",
        data_jurisdiction="us",
        credentials=[],
        actions={
            action_name: ActionCapability(
                fn=_fn, name=action_name, mcp_exposed=mcp_exposed, input_schema=input_schema
            )
        },
    )
    meta._recorded = recorded  # type: ignore[attr-defined]  # test introspection handle
    return meta


def _fake_account(workspace_id: uuid.UUID, connector: str) -> ConnectorAccountRow:
    return ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        connector=connector,
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="cipher-blob",
        delivery_config={"repo": "owner/name"},
        is_active=True,
    )


class FakeConnectorActionProvider:
    """A :class:`ConnectorActionProvider` over a fixed set of fake actions.

    ``credentials_for`` returns a sentinel dict so a test can assert the action
    received decrypted credentials; ``dispatch`` records the call and runs the
    plugin's fn so the result flows back to the loop."""

    def __init__(self, tools: list[ConnectorActionTool]) -> None:
        self._tools = tools
        self.dispatched: list[dict[str, Any]] = []

    async def list_actions(self, workspace_id: uuid.UUID) -> list[ConnectorActionTool]:
        return [t for t in self._tools if t.account.workspace_id == workspace_id]

    def credentials_for(self, tool: ConnectorActionTool) -> dict[str, Any]:
        return {"token": f"decrypted::{tool.account.signing_secret_ciphertext}"}

    async def dispatch(
        self, tool: ConnectorActionTool, *, credentials: dict[str, Any], kwargs: dict[str, Any]
    ) -> Any:
        self.dispatched.append({"tool": tool, "credentials": credentials, "kwargs": kwargs})
        # Run the plugin fn so the real arg-passing + credential injection is
        # exercised end-to-end (the fn records the context it saw).
        return await tool.action.fn(
            SkillContext(llm=_RaisingLlm(), config={}, logger=None, credentials=credentials),
            **kwargs,
        )


def _tool(
    plugin: PluginMeta, account: ConnectorAccountRow, *, is_dangerous: bool
) -> ConnectorActionTool:
    action = next(iter(plugin.actions.values()))
    return ConnectorActionTool(
        plugin=plugin, action=action, account=account, is_dangerous=is_dangerous
    )


async def test_connector_action_in_schema_when_workspace_has_account(tmp_path: Path) -> None:
    """The surfaced tool schema includes a workspace's connector action when the
    workspace has that connector account."""
    llm = ScriptedLlm([LoopTurn(content="done", tool_calls=())])
    ws = uuid.uuid4()
    plugin = _fake_action_plugin("github", action_name="open_pr")
    account = _fake_account(ws, "github")
    provider = FakeConnectorActionProvider([_tool(plugin, account, is_dangerous=False)])
    async with memory_session() as session:
        session.add(WorkspaceRow(id=ws, name="ws", region="us-1", safe_mode=False))
        await session.flush()
        run = await _make_run(session, workspace_id=ws)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            connector_actions=provider,
        )
        await orch.run(run=run, workspace_dir=tmp_path)

    names = _tool_names(llm.calls[0]["tools"])
    assert "github__open_pr" in names
    # Built-ins still present.
    assert "file_write" in names
    assert "ask_user_question" in names


async def test_connector_action_excluded_when_workspace_lacks_account(tmp_path: Path) -> None:
    """A workspace with no connector account surfaces no connector tool (the
    provider returns no actions for it)."""
    llm = ScriptedLlm([LoopTurn(content="done", tool_calls=())])
    other_ws = uuid.uuid4()
    plugin = _fake_action_plugin("github", action_name="open_pr")
    account = _fake_account(other_ws, "github")  # belongs to a DIFFERENT workspace
    provider = FakeConnectorActionProvider([_tool(plugin, account, is_dangerous=False)])
    async with memory_session() as session:
        this_ws = uuid.uuid4()
        session.add(WorkspaceRow(id=this_ws, name="ws", region="us-1", safe_mode=False))
        await session.flush()
        run = await _make_run(session, workspace_id=this_ws)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            connector_actions=provider,
        )
        await orch.run(run=run, workspace_dir=tmp_path)

    names = _tool_names(llm.calls[0]["tools"])
    assert "github__open_pr" not in names


async def test_non_dangerous_action_runs_and_feeds_result_back(tmp_path: Path) -> None:
    """The LLM calls a NON-dangerous connector action → it dispatches, and the
    result is fed back to the loop as a tool message. Credentials are resolved
    into the action context."""
    ws = uuid.uuid4()
    plugin = _fake_action_plugin(
        "github",
        action_name="open_pr",
        input_schema={
            "type": "object",
            "required": ["title"],
            "properties": {"title": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    account = _fake_account(ws, "github")
    provider = FakeConnectorActionProvider([_tool(plugin, account, is_dangerous=False)])
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="opening a PR",
                tool_calls=(_tc("github__open_pr", title="Ship it"),),
            ),
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f out.txt"),
                    _tc("file_write", path="out.txt", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        session.add(WorkspaceRow(id=ws, name="ws", region="us-1", safe_mode=False))
        await session.flush()
        run = await _make_run(session, workspace_id=ws)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            connector_actions=provider,
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        # The action was actually dispatched with the LLM's args.
        assert provider.dispatched, "the connector action should have dispatched"
        assert provider.dispatched[0]["kwargs"] == {"title": "Ship it"}
        # Credentials were resolved into the action context (the plugin fn saw them).
        recorded = plugin._recorded  # type: ignore[attr-defined]
        assert recorded["context"].credentials == {"token": "decrypted::cipher-blob"}
        # No approval Decision was created (non-dangerous).
        assert (await session.execute(select(Decision))).first() is None
        # The action result was fed back as a tool message.
        tool_msgs = [
            m
            for call in llm.calls
            for m in call["messages"]
            if m.get("role") == "tool" and "github__open_pr" not in (m.get("content") or "")
        ]
        ok_msgs = [
            m
            for call in llm.calls
            for m in call["messages"]
            if m.get("role") == "tool" and '"echo"' in (m.get("content") or "")
        ]
        assert ok_msgs, "the connector action result should be appended as a tool message"
        assert tool_msgs  # built-ins / action result tool messages exist


async def test_dangerous_action_in_safe_mode_creates_approval_decision(tmp_path: Path) -> None:
    """The key danger gate: the LLM calls a DANGEROUS action while safe_mode=True
    → NO execution, a ``connector_action_approval`` Decision is created, and the
    LLM gets a 'pending approval' tool result."""
    ws = uuid.uuid4()
    plugin = _fake_action_plugin("slack", action_name="post_message")
    account = _fake_account(ws, "slack")
    provider = FakeConnectorActionProvider([_tool(plugin, account, is_dangerous=True)])
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="posting to slack",
                tool_calls=(_tc("slack__post_message", channel="C1", text="hi"),),
            ),
            # After the gate, the loop continues; the model does real file work.
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f out.txt"),
                    _tc("file_write", path="out.txt", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        session.add(WorkspaceRow(id=ws, name="ws", region="us-1", safe_mode=True))
        await session.flush()
        run = await _make_run(session, workspace_id=ws)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            connector_actions=provider,
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        # The dangerous action did NOT execute.
        assert not provider.dispatched, "a dangerous action must not run in safe mode"
        # An approval Decision was created with the action details.
        decision = (await session.execute(select(Decision))).scalar_one()
        assert decision.decision == "connector_action_approval"
        assert decision.payload["plugin"] == "slack"
        assert decision.payload["action"] == "post_message"
        assert decision.payload["is_dangerous"] is True
        assert decision.payload["args"] == {"channel": "C1", "text": "hi"}
        # The LLM was told the action is pending approval.
        pending_msgs = [
            m
            for call in llm.calls
            for m in call["messages"]
            if m.get("role") == "tool" and "needs_approval" in (m.get("content") or "")
        ]
        assert pending_msgs, "the gate must feed back a needs_approval tool result"


async def test_dangerous_action_runs_when_not_in_safe_mode(tmp_path: Path) -> None:
    """A dangerous action with safe_mode OFF runs directly (the gate is danger AND
    safe_mode — neither alone blocks a non-safe-mode workspace)."""
    ws = uuid.uuid4()
    plugin = _fake_action_plugin("slack", action_name="post_message")
    account = _fake_account(ws, "slack")
    provider = FakeConnectorActionProvider([_tool(plugin, account, is_dangerous=True)])
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="posting",
                tool_calls=(_tc("slack__post_message", text="hi"),),
            ),
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f out.txt"),
                    _tc("file_write", path="out.txt", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        session.add(WorkspaceRow(id=ws, name="ws", region="us-1", safe_mode=False))
        await session.flush()
        run = await _make_run(session, workspace_id=ws)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            connector_actions=provider,
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        assert provider.dispatched, "a dangerous action must run when safe_mode is off"
        assert (await session.execute(select(Decision))).first() is None


async def test_loop_without_connector_provider_omits_connector_tools(tmp_path: Path) -> None:
    """Backward-compat: a run with no connector-action provider surfaces no
    connector tool — the loop is unchanged (and matches every legacy caller)."""
    llm = ScriptedLlm([LoopTurn(content="done", tool_calls=())])
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        await orch.run(run=run, workspace_dir=tmp_path)

    names = _tool_names(llm.calls[0]["tools"])
    assert not any("__" in n for n in names)
    assert "file_write" in names
