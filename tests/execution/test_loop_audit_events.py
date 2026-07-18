"""B15 — Agent-loop audit events are emitted into the supervisor outbox.

Before B15 only the chat-completions gateway path emitted audit events: a run could
plan, act, verify, raise a Decision, and terminate without the supervisor audit
stream (drained by :class:`backend.workflow.infrastructure.workers.relay_worker.RelayWorker`) ever
seeing it. These tests pin the high-signal event SET — ``RunStarted``,
``LlmTurn``, ``ToolCall``, ``VerifyRun``, ``DecisionPending``,
``DecisionResolved`` (in ``tests/api/test_checkpoints_audit.py``),
``LoopTerminal`` — and the soft-fail discipline: if the outbox blows up the run
must still complete (the relay can replay; a domain write must NEVER be broken
by audit).

The deterministic ScriptedLlm / NoopSandboxManager test machinery is reused
from ``test_run_orchestrator.py``; this module imports it rather than
re-declaring it so the fixtures stay in one place.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from backend.workflow.application.agent_loop import LoopTurn as LlmLoopTurn
from backend.workflow.application.agent_loop import RunOrchestrator
from backend.workflow.application.audit_events import (
    DecisionPending,
    LlmTurn,
    LoopTerminal,
    RunStarted,
    ToolCall,
    VerifyRun,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from plugin.audit import register_audit_subscriber
from plugin.audit.models import AuditOutboxRecord

from .._support import memory_session
from .test_run_orchestrator import (
    ScriptedLlm,
    _declare_command,
    _make_run,
    _tc,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _wire_audit_subscriber() -> None:
    """These tests assert audit events land in the ``audit_outbox``. That write
    is done by the ``AuditEventSubscriber``, which production wires onto the
    in-process EventBus ONCE per process at app/worker startup
    (``register_audit_subscriber``) — NOT on import. Run in isolation, no
    earlier test has wired it, so the emits fan out to a bus with no audit
    subscriber and nothing persists (green only when some other suite happened
    to start the runtime first). Wire it here so the assertion holds regardless
    of test order. Idempotent — a second call is a no-op."""
    register_audit_subscriber()


def _event_types(rows: list[AuditOutboxRecord]) -> list[str]:
    return [r.event_type for r in rows]


async def _outbox_rows(session: Any) -> list[AuditOutboxRecord]:
    result = await session.execute(select(AuditOutboxRecord).order_by(AuditOutboxRecord.id.asc()))
    return list(result.scalars().all())


# --------------------------------------------------------------------------
# native loop — verified path emits the full event set
# --------------------------------------------------------------------------


async def test_native_verified_run_emits_full_audit_event_set(tmp_path: Path) -> None:
    """A native run that drives plan→act→verify→terminal must emit every
    high-signal event into the outbox: RunStarted, LlmTurn (per round),
    ToolCall (per tool), VerifyRun, LoopTerminal."""
    llm = ScriptedLlm(
        [
            LlmLoopTurn(
                content="working",
                tool_calls=(
                    _declare_command("grep -q 42 answer.txt"),
                    _tc("file_write", path="answer.txt", content="42\n"),
                ),
            ),
            LlmLoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"

        rows = await _outbox_rows(session)
        types = _event_types(rows)
        # RunStarted comes first.
        assert types[0] == RunStarted.DEFAULT_EVENT_TYPE
        # At least one LlmTurn + one ToolCall + a VerifyRun + a terminal.
        assert LlmTurn.DEFAULT_EVENT_TYPE in types
        assert ToolCall.DEFAULT_EVENT_TYPE in types
        assert VerifyRun.DEFAULT_EVENT_TYPE in types
        # Terminal is last and carries outcome=verified.
        assert types[-1] == LoopTerminal.DEFAULT_EVENT_TYPE
        terminal = rows[-1]
        assert terminal.payload["data"]["outcome"] == "verified"
        # RunStarted carries the run_id + workspace_id for stream consumers.
        started = rows[0]
        assert started.payload["data"]["run_id"] == str(run.id)
        assert started.payload["workspace_id"] == str(run.workspace_id)


async def test_native_tool_call_event_carries_tool_name_and_ok_flag(tmp_path: Path) -> None:
    """ToolCall audit rows must carry the tool name + ok flag so a consumer
    can distinguish a successful file_write from a failed one without the rich
    activity row."""
    llm = ScriptedLlm(
        [
            LlmLoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f marker"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            LlmLoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

        rows = await _outbox_rows(session)
        tool_rows = [r for r in rows if r.event_type == ToolCall.DEFAULT_EVENT_TYPE]
        assert tool_rows, "expected at least one tool_call audit event"
        names = {r.payload["data"]["tool"] for r in tool_rows}
        assert "file_write" in names
        # Each ToolCall carries an ``ok`` boolean.
        for r in tool_rows:
            assert isinstance(r.payload["data"]["ok"], bool)


# --------------------------------------------------------------------------
# decision branch — ask_user_question emits DecisionPending + LoopTerminal
# --------------------------------------------------------------------------


async def test_ask_user_question_emits_decision_pending_and_terminal(
    tmp_path: Path,
) -> None:
    """When the work LLM calls ``ask_user_question`` the run pauses on a
    Decision — the outbox MUST see a ``DecisionPending`` + a
    ``LoopTerminal`` with outcome=needs_decision."""
    llm = ScriptedLlm(
        [
            LlmLoopTurn(
                content="",
                tool_calls=(
                    _tc("ask_user_question", question="Which DB?", context="picking storage"),
                ),
            ),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "needs_decision"

        rows = await _outbox_rows(session)
        types = _event_types(rows)
        assert DecisionPending.DEFAULT_EVENT_TYPE in types
        assert types[-1] == LoopTerminal.DEFAULT_EVENT_TYPE
        terminal = rows[-1]
        assert terminal.payload["data"]["outcome"] == "needs_decision"
        pending = next(r for r in rows if r.event_type == DecisionPending.DEFAULT_EVENT_TYPE)
        assert pending.payload["data"]["kind"] == "ask_user_question"
        assert pending.payload["data"]["decision_id"] == str(result.decision_id)


# --------------------------------------------------------------------------
# soft-fail — an audit outbox failure must NOT break the run
# --------------------------------------------------------------------------


async def test_audit_emit_failure_does_not_break_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the outbox enqueue raises, the run must still drive to verified
    (the audit layer's soft-fail contract — exactly like chat completions)."""
    from backend.workflow.application import agent_loop as orch_mod
    from plugin.audit.store import OutboxStore

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("outbox on fire")

    monkeypatch.setattr(OutboxStore, "enqueue", _boom)

    llm = ScriptedLlm(
        [
            LlmLoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f marker"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            LlmLoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orchestrator = orch_mod.RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager()
        )
        result = await orchestrator.run(run=run, workspace_dir=tmp_path)
        # Run completes verified — audit hiccup never breaks the domain write.
        assert result.outcome == "verified"
        # And the outbox is empty (every insert was swallowed).
        rows = await _outbox_rows(session)
        assert rows == []


# --------------------------------------------------------------------------
# sandbox failure — system_error still emits a terminal
# --------------------------------------------------------------------------


async def test_sandbox_failure_emits_terminal_system_error(tmp_path: Path) -> None:
    """An infra-failure terminal (system_error from a failing sandbox acquire)
    still emits RunStarted + LoopTerminal so the stream isn't blind to bad runs."""
    from tests.execution.test_run_orchestrator import FailingSandboxManager

    async with memory_session() as session:
        run = await _make_run(session)
        orchestrator = RunOrchestrator(
            session=session, llm=ScriptedLlm([]), sandbox_manager=FailingSandboxManager()
        )
        result = await orchestrator.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "system_error"

        rows = await _outbox_rows(session)
        types = _event_types(rows)
        assert types[0] == RunStarted.DEFAULT_EVENT_TYPE
        assert types[-1] == LoopTerminal.DEFAULT_EVENT_TYPE
        assert rows[-1].payload["data"]["outcome"] == "system_error"


# --------------------------------------------------------------------------
# uniqueness — each run-attempt emits exactly ONE terminal
# --------------------------------------------------------------------------


async def test_single_terminal_event_per_run(tmp_path: Path) -> None:
    """One LoopTerminal per run-attempt (not per cycle) — consumers count it."""
    llm = ScriptedLlm(
        [
            LlmLoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f marker"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            LlmLoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orchestrator = RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager()
        )
        result = await orchestrator.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

        rows = await _outbox_rows(session)
        terminals = [r for r in rows if r.event_type == LoopTerminal.DEFAULT_EVENT_TYPE]
        assert len(terminals) == 1


# --------------------------------------------------------------------------
# RunStarted carries product_id when bound (for stream consumers)
# --------------------------------------------------------------------------


async def test_run_started_payload_includes_product_id_when_bound(tmp_path: Path) -> None:
    """RunStarted carries product_id (str | None) so stream consumers can
    cluster by product without joining back through ExecutionRun."""
    llm = ScriptedLlm(
        [
            LlmLoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f marker"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            LlmLoopTurn(content="done", tool_calls=()),
        ]
    )
    product_id = uuid.uuid4()
    async with memory_session() as session:
        run = await _make_run(session, product_id=product_id)
        orchestrator = RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager()
        )
        result = await orchestrator.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

        rows = await _outbox_rows(session)
        started = next(r for r in rows if r.event_type == RunStarted.DEFAULT_EVENT_TYPE)
        assert started.payload["data"]["product_id"] == str(product_id)
