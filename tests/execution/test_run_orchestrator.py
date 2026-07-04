"""RunOrchestrator compute-loop tests (execution layer, no HTTP).

A deterministic stub LLM drives the loop; a real host-side
``NoopSandboxManager`` does the file work + runs the verify command
checks. These prove the §11.3 plan → act → verify → iterate loop end to
end without touching a real model or Docker.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.base import ActionCapability, PluginMeta
from backend.extensions.plugin.context import SkillContext
from backend.extensions.skill.loader import SkillLoader
from backend.extensions.skill.tool_binding import INVOKE_SKILL_NAME
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.knowledge.retrieval.knowledge_item import RetrievedKnowledge
from backend.workflow.application.agent_loop import (
    CanonRetriever,
    LoopLlm,
    LoopToolCall,
    LoopTurn,
    RunOrchestrator,
)
from backend.workflow.infrastructure.connector_actions import ConnectorActionTool
from backend.workflow.infrastructure.db import (
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
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow
from backend.workflow.infrastructure.sandbox import NoopSandboxManager, SandboxUnavailable
from backend.workflow.infrastructure.sandbox.protocol import SandboxResult
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


class _PassingBox:
    """A sandbox session whose every command exits 0 — for tests that exercise
    the loop / artifact-capture flow (with SIMULATED file paths) rather than the
    real ruff/mypy/pytest execution a true sandbox would run."""

    @property
    def workspace_mount(self) -> str:
        return "/workspace"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        return b""

    async def write_file(self, rel_path: str, content: bytes) -> None:
        return None

    async def list_dir(self, rel_path: str) -> list[str]:
        return []


class PassingSandboxManager:
    """Acquire returns a box whose commands all pass — keeps loop/artifact tests
    focused on their subject (not on running real lint/tests against the
    simulated, non-existent file paths the test scripts)."""

    async def acquire(self, project_id: uuid.UUID, workspace_path: str) -> Any:
        return _PassingBox()

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

    async def retrieve_structured(self, signals: str) -> list[RetrievedKnowledge]:
        return [RetrievedKnowledge(text=t) for t in await self.retrieve_for_signals(signals)]


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


# --------------------------------------------------------------------------
# Cooperative cancel — the loop stops at a turn boundary when the run is
# cancelled mid-flight, instead of burning the round budget (dogfood dd2bd3a3).
# --------------------------------------------------------------------------


async def test_cancelled_run_stops_before_any_turn(tmp_path: Path) -> None:
    """A run cancelled before the loop starts dispatches NO LLM turn — the
    cooperative cancel check fires on cycle 0. Maps to no status transition
    (needs_decision); the run stays CANCELLED."""
    llm = ScriptedLlm([])  # raises if a turn is requested
    async with memory_session() as session:
        run = await _make_run(session)
        run.status = RunStatus.CANCELLED
        await session.flush()
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "needs_decision"
        assert "cancelled" in result.summary.lower()
        assert llm.calls == []  # NO turn dispatched
        status = await session.scalar(select(ExecutionRun.status).where(ExecutionRun.id == run.id))
        assert status is RunStatus.CANCELLED


async def test_cancel_between_turns_stops_loop(tmp_path: Path) -> None:
    """A run cancelled DURING the loop stops at the NEXT turn boundary — not at
    round-budget exhaustion. The LLM cancels the run on its first turn (and
    returns a non-terminal turn); the loop must not dispatch a second turn."""
    from sqlalchemy import update

    class _CancelOnFirstTurnLlm:
        def __init__(self, session: Any, run_id: Any) -> None:
            self._session = session
            self._run_id = run_id
            self.calls = 0

        async def complete(
            self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
        ) -> LoopTurn:
            self.calls += 1
            if self.calls == 1:
                await self._session.execute(
                    update(ExecutionRun)
                    .where(ExecutionRun.id == self._run_id)
                    .values(status=RunStatus.CANCELLED)
                )
                await self._session.flush()
                # Non-terminal turn (no declare_verification) → loop would
                # continue to a 2nd cycle if not for the cancel check.
                return LoopTurn(content="still working…", tool_calls=())
            raise AssertionError("loop dispatched a 2nd turn after cancel — cancel not honored")

    async with memory_session() as session:
        run = await _make_run(session)
        llm = _CancelOnFirstTurnLlm(session, run.id)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert llm.calls == 1  # stopped at the turn boundary after the cancel
        assert result.outcome == "needs_decision"
        status = await session.scalar(select(ExecutionRun.status).where(ExecutionRun.id == run.id))
        assert status is RunStatus.CANCELLED


# --------------------------------------------------------------------------
# Verified-deliverable summary — titled by the founder intent, NOT the
# agent's raw streaming narration.
# --------------------------------------------------------------------------


async def test_verified_summary_titled_by_intent_bodied_by_changed_files() -> None:
    """The deliverable summary's FIRST line becomes the PR title (via
    ``_split_summary``) and the settle note's title. A coding-agent executor's
    ``--print`` output is raw first-person streaming narration ("Let me check…
    Now I'll… Phase 1 (RED)…") with chunk-join whitespace artifacts — slop in a
    user-facing deliverable summary / PR body (live dogfood F4). The fix: title
    from the founder intent, body from the DETERMINISTIC list of changed files,
    and never dump the agent's raw prose into the summary (it stays in the
    ``llm_turn`` activity for debugging).
    """
    from types import SimpleNamespace

    from backend.workflow.application.run_persistence import _compose_verified_summary

    run = SimpleNamespace(
        payload={"intent_text": "Add a TTL cache utility to the backend.\n\nDetails: monotonic."}
    )
    agent_output = (
        "Let me check the existing backend structure first.\n"
        "Now I'll follow TDD.Phase 1 (RED).All 3 tests pass.\n"
        "<verification-contract>\n"
        '{"checks": [{"kind": "command", "command": "uv run pytest -q"}]}\n'
        "</verification-contract>"
    )
    written_paths = ["backend/cache.py", "tests/test_cache.py"]
    summary = _compose_verified_summary(run, agent_output, written_paths)  # type: ignore[arg-type]

    title = summary.splitlines()[0].strip()
    assert title == "Add a TTL cache utility to the backend."
    # Body lists what actually changed — deterministic, no slop.
    assert "backend/cache.py" in summary
    assert "tests/test_cache.py" in summary
    # The agent's first-person narration is NOT dumped into the summary.
    assert "Let me check the existing backend structure" not in summary
    assert "Now I'll follow TDD" not in summary
    assert "Phase 1 (RED)" not in summary
    # The verification-contract block never leaks either.
    assert "<verification-contract>" not in summary
    assert "uv run pytest" not in summary


async def test_compose_verified_summary_falls_back_to_cleaned_prose_without_paths() -> None:
    """When no changed-file list is available (rare — a non-file deliverable),
    fall back to the agent prose, but strip the contract block and repair the
    streaming chunk-join whitespace artifacts ("done.Next" → "done. Next")."""
    from types import SimpleNamespace

    from backend.workflow.application.run_persistence import _compose_verified_summary

    run = SimpleNamespace(payload={"intent_text": "Investigate the flake."})
    summary = _compose_verified_summary(run, "Found the cause.Fixed the race.", None)  # type: ignore[arg-type]
    assert summary.splitlines()[0].strip() == "Investigate the flake."
    # whitespace-join artifact repaired:
    assert "Found the cause. Fixed the race." in summary
    assert "cause.Fixed" not in summary


async def test_compose_verified_summary_falls_back_when_no_intent() -> None:
    from types import SimpleNamespace

    from backend.workflow.application.run_persistence import _compose_verified_summary

    run = SimpleNamespace(payload={})
    summary = _compose_verified_summary(run, "did the thing", ["a.py"])  # type: ignore[arg-type]
    # No intent → still a non-empty, clean fallback title + the changed file.
    assert summary.splitlines()[0].strip() == "Delivered change"
    assert "a.py" in summary


async def test_compose_verified_summary_weaves_verification_result() -> None:
    """R1 — the report summary is thin (intent title + bare file list). When a
    passing ``VerificationResult`` is available, weave its outcome into the
    summary DETERMINISTICALLY (no LLM): how many checks passed, by derived
    friendly category (tests / lint / format / types), plus the acceptance
    judge. The raw command strings are NOT echoed (F4 anti-slop — the
    verification-contract block must never leak into the user-facing summary)."""
    from types import SimpleNamespace

    from backend.workflow.application.run_persistence import _compose_verified_summary

    run = SimpleNamespace(payload={"intent_text": "Add a TTL cache utility."})
    verdict = SimpleNamespace(
        result={
            "command_results": [
                {"command": "uv run ruff check backend/", "passed": True},
                {"command": "uv run ruff format --check backend/", "passed": True},
                {"command": "uv run pytest -q", "passed": True},
            ],
            "judge": {"passed": True, "reasoning": "meets the intent"},
        },
    )
    summary = _compose_verified_summary(
        run,
        "raw narration",
        ["backend/cache.py", "tests/test_cache.py"],
        verdict,  # type: ignore[arg-type]
    )

    # Intent still titles it; the changed files still list.
    assert summary.splitlines()[0].strip() == "Add a TTL cache utility."
    assert "backend/cache.py" in summary
    # The verification OUTCOME is woven in, narratively.
    assert "Verified: 3 checks passed" in summary
    # Derived friendly categories, NOT the raw command strings.
    assert "tests" in summary and "lint" in summary and "format" in summary
    assert "uv run pytest" not in summary
    assert "ruff check" not in summary
    # The acceptance judge is surfaced.
    assert "Acceptance check passed." in summary


async def test_compose_verified_summary_no_verdict_unchanged() -> None:
    """Backward-compat: with no verdict (the existing 3-arg callers), the summary
    is exactly the prior title + changed-file body — no verification line."""
    from types import SimpleNamespace

    from backend.workflow.application.run_persistence import _compose_verified_summary

    run = SimpleNamespace(payload={"intent_text": "Add a cache."})
    summary = _compose_verified_summary(run, "narration", ["a.py"])  # type: ignore[arg-type]
    assert "Verified:" not in summary
    assert summary == "Add a cache.\n\nChanged files:\n- a.py"


async def test_executor_artifact_refs_flow_into_deliverable(tmp_path: Path) -> None:
    """Coding-agent executors write files in the worker's clone — captured
    worker-side as the executor task's ``artifact_refs``, NOT via the loop's
    ``file_write`` tools. So the loop's ``written_paths`` stayed empty and the
    verified ``Deliverable.artifact_refs`` came out ``[]`` (live: PR/settle
    showed no changed-file list even though files shipped in the git branch).

    The ExecutorAdapter now surfaces the captured paths on the turn
    (``LoopTurn.artifact_refs``); the loop merges them into ``written_paths`` so
    the deliverable records what actually changed.
    """
    declare = LoopToolCall(
        id="e30-declare-verification",
        name="declare_verification",
        arguments={"checks": [{"kind": "command", "command": "true"}]},
    )
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="did it in one shot",
                tool_calls=(declare,),
                artifact_refs=("backend/common/bytesize.py", "tests/common/test_bytesize.py"),
            ),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        # PassingSandboxManager (not Noop): the artifact_refs here are SIMULATED
        # paths that don't exist on disk, so the L1 mandatory ruff/mypy gates
        # (added in assemble_contract) would fail running against them on a real
        # host-exec box. This test is about artifact flow, not lint — so use a
        # box whose commands pass.
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=PassingSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        assert result.written_paths == [
            "backend/common/bytesize.py",
            "tests/common/test_bytesize.py",
        ]
        deliverable = (await session.execute(select(Deliverable))).scalar_one()
        assert deliverable.payload.get("artifact_refs") == [
            "backend/common/bytesize.py",
            "tests/common/test_bytesize.py",
        ]


# --------------------------------------------------------------------------
# Executor path — the synthesized declare_verification is TERMINAL.
# --------------------------------------------------------------------------


async def test_executor_synthesized_declare_is_terminal_and_verifies(tmp_path: Path) -> None:
    """A coding-agent executor (claude_code / codex / opencode) is single-shot:
    it does ALL its work in one turn and ends with a ``<verification-contract>``
    block, which the ExecutorAdapter turns into a synthesized
    ``declare_verification`` tool call (id ``e30-declare-verification``) on
    EVERY turn. The loop only reaches verification when ``tool_calls`` is empty
    — which the executor never returns — so without treating the synthesized
    declare as terminal the loop spins to its round cap and never verifies
    (live dogfood: claude/sonnet emitted the contract reliably every turn → the
    run round-capped instead of producing a deliverable).

    The fix: once the executor's synthesized declare has registered a contract,
    THAT turn is terminal → proceed straight to verification. This test scripts
    a SINGLE such turn with NO trailing empty-tool_calls turn; if the loop still
    demands one, ScriptedLlm is asked for a second turn and raises.
    """
    declare = LoopToolCall(
        id="e30-declare-verification",
        name="declare_verification",
        arguments={"checks": [{"kind": "command", "command": "true"}]},
    )
    llm = ScriptedLlm([LoopTurn(content="did the work in one shot", tool_calls=(declare,))])
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        # Exactly ONE work turn — the synthesized declare was terminal, no
        # wasteful re-prompt of the single-shot executor.
        assert len(llm.calls) == 1

        vr = (await session.execute(select(VerificationResult))).scalar_one()
        assert vr.outcome is VerificationOutcome.PASSED


# --------------------------------------------------------------------------
# B7 — verify-first gate: a write BEFORE declare_verification is refused;
# the model must declare first, then writes work (the new discipline).
# --------------------------------------------------------------------------


async def test_write_before_declare_is_refused_then_declare_then_verified(
    tmp_path: Path,
) -> None:
    """The work LLM tries to write first (refused with an actionable error),
    then declares verification, then writes (now succeeds), then summarises →
    verified. Proves the gate refuses the premature write yet the run still
    reaches a verified terminal once the model follows the discipline."""
    llm = ScriptedLlm(
        [
            # Turn 1: write before declaring → refused, no file on disk.
            LoopTurn(
                content="writing first",
                tool_calls=(_tc("file_write", path="answer.txt", content="42\n"),),
            ),
            # Turn 2: declare, then write (now unlocked).
            LoopTurn(
                content="declaring then writing",
                tool_calls=(
                    _declare_command("grep -q 42 answer.txt"),
                    _tc("file_write", path="answer.txt", content="42\n"),
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
        assert result.written_paths == ["answer.txt"]
        assert (tmp_path / "answer.txt").read_text() == "42\n"

        # The first (premature) write was fed back as a refusal tool message
        # naming declare_verification — not silently dropped.
        refusal_msgs = [
            m
            for call in llm.calls
            for m in call["messages"]
            if m.get("role") == "tool"
            and "declare_verification" in (m.get("content") or "")
            and "ERROR" in (m.get("content") or "")
        ]
        assert refusal_msgs, "the premature write must be refused with an actionable error"


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


async def test_ask_user_question_tool_schema_advertises_options() -> None:
    """B11a: the work LLM's ``ask_user_question`` tool advertises a structured
    optional ``options`` field (an array of plain strings) so the model can
    present concrete choices to the founder, not just free text."""
    from backend.workflow.application.tool_registry import ASK_USER_QUESTION_TOOL

    params = ASK_USER_QUESTION_TOOL["function"]["parameters"]
    assert params["type"] == "object"
    props = params["properties"]
    assert "options" in props
    options_schema = props["options"]
    assert options_schema["type"] == "array"
    # v1 shape: a plain list of strings (one chosen option string is the answer).
    assert options_schema["items"]["type"] == "string"
    # ``options`` is OPTIONAL — free-text mode (no options) must keep working,
    # so the required list MUST NOT grow beyond the existing ``question``.
    assert params["required"] == ["question"]


async def test_ask_user_question_with_options_persists_them_on_decision(
    tmp_path: Path,
) -> None:
    """B11a: when the work LLM calls ``ask_user_question`` WITH ``options``, the
    minted Decision's payload carries the offered choices so the Decisions UI
    can render them as a single-select."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="I need the founder to pick.",
                tool_calls=(
                    _tc(
                        "ask_user_question",
                        question="Which database should I target?",
                        options=["postgres", "sqlite", "mysql"],
                    ),
                ),
            ),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "needs_decision"
        decision = (await session.execute(select(Decision))).scalar_one()
        assert decision.payload.get("question")
        assert decision.payload.get("options") == ["postgres", "sqlite", "mysql"]


async def test_ask_user_question_without_options_omits_field(tmp_path: Path) -> None:
    """B11a regression: a free-text ``ask_user_question`` (no options) must
    NOT plant a stray ``options`` key on the Decision payload — the existing
    free-text resolve path stays unaffected."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="I'm stuck.",
                tool_calls=(_tc("ask_user_question", question="What should I do here?"),),
            ),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        await orch.run(run=run, workspace_dir=tmp_path)

        decision = (await session.execute(select(Decision))).scalar_one()
        assert "options" not in decision.payload


async def test_ask_user_question_drops_non_string_options(tmp_path: Path) -> None:
    """B11a: defensive — if the LLM hands us non-string entries in ``options``
    (provider drift, type confusion), the orchestrator coerces / drops them so
    only clean strings survive onto the Decision payload."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="picking",
                tool_calls=(
                    _tc(
                        "ask_user_question",
                        question="Pick one",
                        options=["good", 42, None, "  ", "other"],
                    ),
                ),
            ),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        await orch.run(run=run, workspace_dir=tmp_path)

        decision = (await session.execute(select(Decision))).scalar_one()
        assert decision.payload.get("options") == ["good", "other"]


async def test_no_contract_declared_routes_to_decision(tmp_path: Path) -> None:
    """Work that finishes without ever declaring a contract is never a
    silent pass — it becomes a human-review Decision.

    Under the B7 verify-first gate the premature ``file_write`` is REFUSED
    (so no file lands and ``written_paths`` stays empty), the loop nudges the
    model to declare-then-do (up to ``MAX_NO_WORK_NUDGES``), and when the model
    still never declares it routes to the human-review Decision — still never a
    silent pass."""
    llm = ScriptedLlm(
        [
            # Refused — no contract declared yet, so the write does not land.
            LoopTurn(content="", tool_calls=(_tc("file_write", path="foo.txt", content="bar"),)),
            # Two no-work turns absorbed by the nudge, then a third settles to verify.
            LoopTurn(content="I think I'm done", tool_calls=()),
            LoopTurn(content="still nothing", tool_calls=()),
            LoopTurn(content="giving up", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "needs_decision"
        decision = (await session.execute(select(Decision))).scalar_one()
        assert decision.decision == "human_review_required"
        # The premature write was refused — nothing landed on disk.
        assert not (tmp_path / "foo.txt").exists()


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


# --------------------------------------------------------------------------
# B6 — knowledge informs the WORK: canon relevant to the run intent is seeded
# into the agent's initial context BEFORE the act/verify cycle (RC-2)
# --------------------------------------------------------------------------


def _all_message_text(messages: list[dict[str, Any]]) -> str:
    """Flatten every message's content into one blob for substring assertions."""
    parts: list[str] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


async def test_knowledge_seeded_into_initial_context_on_turn_1(tmp_path: Path) -> None:
    """A workspace whose canon matches the intent → the seeded pattern text is
    present in the messages the loop LLM receives on its FIRST turn (it was not
    before B6). The verify-time retriever fold (B3) still happens, so the stub
    must also script the judge call for the folded criterion."""
    retriever = StubRetriever(["always pin dependency versions"])
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_judge("the file pins deps"),
                    _tc("file_write", path="deps.txt", content="pkg==1.0"),
                ),
            ),
            LoopTurn(content="declared deps", tool_calls=()),
            # judge call (tools=None) for the declared + folded criteria
            LoopTurn(content='{"passed": true}', tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session, intent="add a dependency to the project")
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            retriever=retriever,
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

    assert result.outcome == "verified"
    # Turn 1 saw the seeded knowledge in its messages.
    first_messages = llm.calls[0]["messages"]
    blob = _all_message_text(first_messages)
    assert "pin dependency versions" in blob
    # The retriever was queried with the STABLE run intent (not written_paths).
    assert retriever.queried, "retriever should be queried for the seed at loop start"
    assert "add a dependency to the project" in retriever.queried[0]


async def test_no_seed_message_for_empty_or_absent_knowledge(tmp_path: Path) -> None:
    """Empty-knowledge workspace AND no retriever both inject NOTHING — the
    initial messages are byte-identical to the no-knowledge baseline."""
    baseline = LoopTurn(content="nothing to do", tool_calls=())

    async def _first_messages(retriever: CanonRetriever | None) -> list[dict[str, Any]]:
        llm = ScriptedLlm([baseline])
        async with memory_session() as session:
            run = await _make_run(session, intent="some work")
            orch = RunOrchestrator(
                session=session,
                llm=llm,
                sandbox_manager=NoopSandboxManager(),
                retriever=retriever,
            )
            await orch.run(run=run, workspace_dir=tmp_path)
        return llm.calls[0]["messages"]

    no_retriever = await _first_messages(None)
    empty_retriever = await _first_messages(StubRetriever([]))

    # No seed message in either case → identical initial message lists.
    assert no_retriever == empty_retriever
    blob = _all_message_text(no_retriever)
    assert "established patterns" not in blob.lower()


async def test_suggested_skill_hint_seeded_into_initial_context(tmp_path: Path) -> None:
    """B9a — when the orchestrator is given a frame-matched ``suggested_skill``,
    the loop's FIRST-turn context nudges the model to invoke it via invoke_skill
    (previously the frame dict was written and never consumed)."""
    llm = ScriptedLlm([LoopTurn(content="nothing to do", tool_calls=())])
    async with memory_session() as session:
        run = await _make_run(session, intent="draft a PRD for onboarding")
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            suggested_skill="prd-writer",
            suggested_skill_description="Draft a product requirements document",
        )
        await orch.run(run=run, workspace_dir=tmp_path)

    blob = _all_message_text(llm.calls[0]["messages"])
    assert "prd-writer" in blob
    assert "Draft a product requirements document" in blob
    assert "invoke_skill" in blob


async def test_no_suggested_skill_hint_when_unset(tmp_path: Path) -> None:
    """No ``suggested_skill`` → no hint message (loop unchanged from today)."""
    llm = ScriptedLlm([LoopTurn(content="nothing to do", tool_calls=())])
    async with memory_session() as session:
        run = await _make_run(session, intent="some work")
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        await orch.run(run=run, workspace_dir=tmp_path)

    blob = _all_message_text(llm.calls[0]["messages"]).lower()
    assert "suggested skill" not in blob


# --------------------------------------------------------------------------
# D1b — the DESIGN stage of a design_then_impl pipeline must be told (on turn 1)
# to write a SPEC, not finished code. The native loop seeds a spec-only
# directive into the initial context when the run is the design stage; the
# single + impl runs are unchanged.
# --------------------------------------------------------------------------


async def _make_run_with_payload(session: AsyncSession, *, payload: dict[str, Any]) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=None,
        status=RunStatus.RUNNING,
        payload=payload,
    )
    session.add(run)
    await session.flush()
    return run


async def _first_turn_blob(tmp_path: Path, payload: dict[str, Any]) -> str:
    """Drive the native loop once for a run with ``payload`` and return the
    flattened text of the messages the LLM saw on its first turn."""
    llm = ScriptedLlm([LoopTurn(content="nothing to do", tool_calls=())])
    async with memory_session() as session:
        run = await _make_run_with_payload(session, payload=payload)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        await orch.run(run=run, workspace_dir=tmp_path)
    return _all_message_text(llm.calls[0]["messages"])


async def test_native_design_stage_seeds_spec_only_directive(tmp_path: Path) -> None:
    from backend.workflow.application.agent_loop import _DESIGN_SPEC_DIRECTIVE

    # First run of a design_then_impl pipeline (no explicit stage) → DESIGN.
    blob = await _first_turn_blob(
        tmp_path,
        {
            "intent_text": "build a JSON-backed key/value store",
            "frame": {"pipeline": "design_then_impl"},
        },
    )
    assert _DESIGN_SPEC_DIRECTIVE in blob


async def test_native_single_pipeline_has_no_spec_only_directive(tmp_path: Path) -> None:
    from backend.workflow.application.agent_loop import _DESIGN_SPEC_DIRECTIVE

    blob = await _first_turn_blob(
        tmp_path,
        {"intent_text": "ship the feature", "frame": {"pipeline": "single"}},
    )
    assert _DESIGN_SPEC_DIRECTIVE not in blob


async def test_native_impl_stage_has_no_spec_only_directive(tmp_path: Path) -> None:
    from backend.workflow.application.agent_loop import _DESIGN_SPEC_DIRECTIVE

    blob = await _first_turn_blob(
        tmp_path,
        {
            "intent_text": "build a JSON-backed key/value store",
            "frame": {"pipeline": "design_then_impl"},
            "stage": "impl",
        },
    )
    assert _DESIGN_SPEC_DIRECTIVE not in blob


async def test_native_no_frame_has_no_spec_only_directive(tmp_path: Path) -> None:
    from backend.workflow.application.agent_loop import _DESIGN_SPEC_DIRECTIVE

    blob = await _first_turn_blob(tmp_path, {"intent_text": "some work"})
    assert _DESIGN_SPEC_DIRECTIVE not in blob


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
# B5b — connector @p.action surfaced as loop tools
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


def _tool(plugin: PluginMeta, account: ConnectorAccountRow) -> ConnectorActionTool:
    action = next(iter(plugin.actions.values()))
    return ConnectorActionTool(plugin=plugin, action=action, account=account)


async def test_connector_action_in_schema_when_workspace_has_account(tmp_path: Path) -> None:
    """The surfaced tool schema includes a workspace's connector action when the
    workspace has that connector account."""
    llm = ScriptedLlm([LoopTurn(content="done", tool_calls=())])
    ws = uuid.uuid4()
    plugin = _fake_action_plugin("github", action_name="open_pr")
    account = _fake_account(ws, "github")
    provider = FakeConnectorActionProvider([_tool(plugin, account)])
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
    provider = FakeConnectorActionProvider([_tool(plugin, account)])
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
    provider = FakeConnectorActionProvider([_tool(plugin, account)])
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


# --------------------------------------------------------------------------
# Lift 0a — per-call DangerAnalyzer evaluator removed (YAGNI rollback)
# Lift 0c — load-time DangerAnalyzer + Safe Mode gate removed (asserted in
# tests/glue/test_lift0c_no_static_danger_analyzer.py)
# --------------------------------------------------------------------------


def test_orchestrator_module_does_not_re_export_m2_danger_symbols() -> None:
    """Surface delta: the M2 evaluator symbols (``ActionDangerEvaluator``,
    ``DangerVerdict``, ``StaticActionDangerEvaluator``) are no longer
    re-exported from the workflow loop modules. Mirrors the deletion of
    ``backend.execution.action_danger`` (the import would fail before this
    assertion is even reached, but the assertion locks the surface delta in
    place against accidental re-introduction).

    Post-Lift H3c the legacy ``backend.execution.orchestrator`` shim is
    deleted; the loop now lives in :mod:`backend.workflow.application` (H2a
    decomposition). Assert across every successor module."""
    from backend.workflow.application import agent_loop, run_persistence, tool_registry
    from backend.workflow.domain import emit_deliverable

    for mod in (agent_loop, tool_registry, run_persistence, emit_deliverable):
        assert not hasattr(mod, "ActionDangerEvaluator"), mod.__name__
        assert not hasattr(mod, "DangerVerdict"), mod.__name__
        assert not hasattr(mod, "StaticActionDangerEvaluator"), mod.__name__


def test_action_danger_module_is_deleted() -> None:
    """Surface delta: the per-call evaluator module is gone — importing it
    raises ``ModuleNotFoundError``. Keeps Lift 0a from silently regressing
    into a "module-still-present-but-unused" half-state."""
    import importlib

    try:
        importlib.import_module("backend.execution.action_danger")
    except ModuleNotFoundError:
        return
    raise AssertionError("backend.execution.action_danger must be deleted (Lift 0a YAGNI rollback)")


# --------------------------------------------------------------------------
# Connector surface — two read-only @p.action's kept on the agent surface
# (their surface stays; only the M2 per-call gate around them was removed)
# --------------------------------------------------------------------------


async def test_real_github_list_issues_action_appears_in_tool_schema(
    tmp_path: Path,
) -> None:
    """Presence delta: ``github__list_issues`` (the new M2 read action) is
    surfaced in the agent's tool list when the workspace has a github account.
    Asserts against the real github plugin meta (not a fake) so the test is
    tied to the deployed action declaration."""
    from plugin.github import plugin as github_module

    ws = uuid.uuid4()
    meta = github_module.p.meta
    # The new action is declared (this is the static presence assertion).
    assert "list_issues" in meta.actions, "M2: github plugin must declare list_issues @p.action"
    assert meta.actions["list_issues"].mcp_exposed is True

    account = _fake_account(ws, "github")
    tool = ConnectorActionTool(
        plugin=meta,
        action=meta.actions["list_issues"],
        account=account,
    )
    provider = FakeConnectorActionProvider([tool])
    llm = ScriptedLlm([LoopTurn(content="done", tool_calls=())])
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
    assert "github__list_issues" in names


async def test_real_sentry_list_issues_action_appears_in_tool_schema(
    tmp_path: Path,
) -> None:
    """Presence delta for sentry's M2 read action."""
    from plugin.sentry import plugin as sentry_module

    ws = uuid.uuid4()
    meta = sentry_module.p.meta
    assert "list_issues" in meta.actions, "M2: sentry plugin must declare list_issues @p.action"
    assert meta.actions["list_issues"].mcp_exposed is True

    account = _fake_account(ws, "sentry")
    tool = ConnectorActionTool(
        plugin=meta,
        action=meta.actions["list_issues"],
        account=account,
    )
    provider = FakeConnectorActionProvider([tool])
    llm = ScriptedLlm([LoopTurn(content="done", tool_calls=())])
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
    assert "sentry__list_issues" in names


async def test_new_action_absent_when_workspace_lacks_account(tmp_path: Path) -> None:
    """A workspace WITHOUT an account for the connector surfaces no tool —
    the negative half of the presence delta. (The new actions are not blanket-
    exposed; they need an active workspace account, same as the existing
    connector tools.)"""
    from plugin.github import plugin as github_module

    meta = github_module.p.meta
    other_ws = uuid.uuid4()
    account = _fake_account(other_ws, "github")  # belongs to a different ws
    tool = ConnectorActionTool(
        plugin=meta,
        action=meta.actions["list_issues"],
        account=account,
    )
    provider = FakeConnectorActionProvider([tool])
    llm = ScriptedLlm([LoopTurn(content="done", tool_calls=())])
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
    assert "github__list_issues" not in names


# --------------------------------------------------------------------------
# M2 — successful call exercises real dispatch through the @p.action path
# --------------------------------------------------------------------------


async def test_real_github_list_issues_dispatches_through_real_pluginrunner(
    tmp_path: Path,
) -> None:
    """Exercise the REAL ``@p.action`` dispatch path: the agent loop calls
    ``github__list_issues``, the orchestrator hands the call to the real
    :class:`PluginRunner` via the production
    :class:`ConnectorActionResolver`, and the action's real function runs and
    returns its shaped result back to the LLM as a tool message.

    External HTTP is mocked at the SDK boundary (respx mocks httpx —  no real
    GitHub call), so we exercise the framework's dispatch + the action body's
    own filtering/shaping, not a fake provider stub."""
    import httpx as _httpx
    import respx

    from backend.router.accounts.crypto import CredentialCipher
    from backend.workflow.infrastructure.connector_actions import ConnectorActionResolver
    from plugin.github import plugin as github_module

    ws = uuid.uuid4()
    meta = github_module.p.meta
    # Seed a real ConnectorAccountRow (its encrypted secret is what the
    # resolver decrypts at dispatch time).
    cipher = CredentialCipher(os.urandom(32))
    async with memory_session() as session:
        session.add(WorkspaceRow(id=ws, name="ws", region="us-1", safe_mode=False))
        account = ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            connector="github",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("ghp_test_token"),
            delivery_config={},
            is_active=True,
        )
        session.add(account)
        await session.flush()

        resolver = ConnectorActionResolver(
            session=session,
            plugins_by_name={"github": meta},
            cipher=cipher,
        )
        # The LLM script: call list_issues, then do trivial verifiable file work.
        llm = ScriptedLlm(
            [
                LoopTurn(
                    content="reading",
                    tool_calls=(_tc("github__list_issues", repo="o/r", state="open", limit=5),),
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
        run = await _make_run(session, workspace_id=ws)
        with respx.mock(assert_all_called=False) as r:
            # The real GithubClient.list_issues hits this endpoint.
            r.get("https://api.github.com/repos/o/r/issues").mock(
                return_value=_httpx.Response(
                    200,
                    json=[
                        {
                            "number": 1,
                            "title": "Bug",
                            "state": "open",
                            "html_url": "https://github.com/o/r/issues/1",
                            "user": {"login": "octo"},
                        },
                        # A PR mixed into the issues list — must be filtered.
                        {
                            "number": 2,
                            "title": "PR",
                            "state": "open",
                            "html_url": "https://github.com/o/r/pull/2",
                            "pull_request": {"url": "x"},
                            "user": {"login": "octo"},
                        },
                    ],
                )
            )
            orch = RunOrchestrator(
                session=session,
                llm=llm,
                sandbox_manager=NoopSandboxManager(),
                connector_actions=resolver,
            )
            result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        # The action's result was fed back as a tool message — assert the
        # SHAPED result the action function builds (filtered PR out, trimmed
        # to the issue fields).
        tool_msgs = [
            m
            for call in llm.calls
            for m in call["messages"]
            if m.get("role") == "tool" and '"issues"' in (m.get("content") or "")
        ]
        assert tool_msgs, "the action result must be appended as a tool message"
        body = tool_msgs[0]["content"]
        assert '"count": 1' in body, f"PR should have been filtered out; got {body}"
        assert "Bug" in body
        assert "PR" not in body or '"number": 2' not in body
