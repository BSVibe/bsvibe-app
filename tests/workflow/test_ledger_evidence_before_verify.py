"""A run that says "done" without evidence for its ledger is asked for it — before verifying.

Prod #1204 re-run (d8ae99bd, 2026-10-27): round 1's 16 checks all passed and the round still
failed, because the agent declared no ``requirement_evidence`` at all. That cost a 16-minute
verification round to learn something the loop could see before spending it: the declaration
names none of the items the ledger owes.

So the loop asks once, for exactly the items that have no entry, and only then verifies.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

import backend.workflow.infrastructure.delivery.db  # noqa: F401 — terminal contract tables
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.domain.requirements import REQUIREMENTS_KEY, unevidenced
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from tests._support import memory_session

_LEDGER = [
    {"id": "R1", "text": "add the rotate route", "kind": "do", "source": "issue"},
    {"id": "R2", "text": "foreign workspace is 404", "kind": "guard", "source": "issue"},
    {"id": "R3", "text": "do not touch the PWA", "kind": "constraint", "source": "issue"},
]


def test_unevidenced_names_owed_items_without_an_entry() -> None:
    evidence = [{"requirement_id": "R1", "status": "done", "paths": ["a.py"]}]
    assert [i["id"] for i in unevidenced(_LEDGER, evidence)] == ["R2"]
    deferred = [*evidence, {"requirement_id": "R2", "status": "deferred", "note": "later"}]
    assert unevidenced(_LEDGER, deferred) == []
    assert [i["id"] for i in unevidenced(_LEDGER, [])] == ["R1", "R2"]  # never the constraint


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


class _ScriptedLlm:
    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)
        self.seen: list[list[dict[str, Any]]] = []

    async def complete(self, *, messages: list[dict[str, Any]], tools: Any) -> LoopTurn:
        self.seen.append(list(messages))
        if not self._turns:
            raise AssertionError("scripted LLM exhausted — the loop kept going")
        return self._turns.pop(0)


def _tool(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


_EVIDENCE = [
    {"requirement_id": "R1", "paths": ["a.py"]},
    {"requirement_id": "R2", "status": "deferred", "note": "needs the founder"},
]


async def _drive(tmp_path: Path, monkeypatch: Any, turns: list[LoopTurn]) -> tuple[Any, Any]:
    repo = _repo(tmp_path / "ws")
    llm = _ScriptedLlm(turns)
    verified: list[Any] = []
    async with memory_session() as session:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            status=RunStatus.RUNNING,
            payload={"intent_text": "x", REQUIREMENTS_KEY: _LEDGER},
        )
        session.add(run)
        await session.flush()
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())

        async def fake_verify(**kwargs: Any) -> VerificationResult:
            verified.append(kwargs["contract"])
            vr = VerificationResult(
                id=uuid.uuid4(),
                run_id=run.id,
                work_step_id=kwargs["work_step"].id,
                workspace_id=run.workspace_id,
                outcome=VerificationOutcome.PASSED,
                contract={},
                result={},
            )
            session.add(vr)
            await session.flush()
            return vr

        monkeypatch.setattr(orch, "_verify", fake_verify)
        await orch.run(run=run, workspace_dir=repo)
    return llm, verified


def _reminders(llm: _ScriptedLlm) -> list[str]:
    return [
        m["content"]
        for m in llm.seen[-1]
        if m.get("role") == "user" and "REQUIREMENT EVIDENCE MISSING" in str(m.get("content"))
    ]


async def test_done_without_evidence_is_asked_for_it_before_any_verification(
    tmp_path: Path, monkeypatch: Any
) -> None:
    turns = [
        LoopTurn(
            content="declare + write",
            tool_calls=(
                _tool("declare_verification", checks=[{"kind": "command", "command": "true"}]),
                _tool("file_write", path="a.py", content="x = 1\n"),
            ),
        ),
        LoopTurn(content="done", tool_calls=()),
        LoopTurn(
            content="evidence",
            tool_calls=(
                _tool(
                    "declare_verification",
                    checks=[{"kind": "command", "command": "true"}],
                    requirement_evidence=_EVIDENCE,
                ),
            ),
        ),
        LoopTurn(content="done", tool_calls=()),
    ]
    llm, verified = await _drive(tmp_path, monkeypatch, turns)

    [reminder] = _reminders(llm)
    assert "R1" in reminder and "R2" in reminder and "R3" not in reminder
    assert len(verified) == 1  # one round — and it carried the evidence
    assert {e["requirement_id"] for e in verified[0].requirement_evidence} == {"R1", "R2"}


async def test_the_reminder_is_asked_once_then_verification_decides(
    tmp_path: Path, monkeypatch: Any
) -> None:
    turns = [
        LoopTurn(
            content="declare + write",
            tool_calls=(
                _tool("declare_verification", checks=[{"kind": "command", "command": "true"}]),
                _tool("file_write", path="a.py", content="x = 1\n"),
            ),
        ),
        LoopTurn(content="done", tool_calls=()),
        LoopTurn(content="still done", tool_calls=()),  # ignores the reminder
    ]
    llm, verified = await _drive(tmp_path, monkeypatch, turns)

    assert len(_reminders(llm)) == 1
    assert len(verified) == 1  # not a loop of reminders: the gate's verdict takes over


async def test_a_run_whose_evidence_is_complete_is_not_reminded(
    tmp_path: Path, monkeypatch: Any
) -> None:
    turns = [
        LoopTurn(
            content="declare + write",
            tool_calls=(
                _tool(
                    "declare_verification",
                    checks=[{"kind": "command", "command": "true"}],
                    requirement_evidence=_EVIDENCE,
                ),
                _tool("file_write", path="a.py", content="x = 1\n"),
            ),
        ),
        LoopTurn(content="done", tool_calls=()),
    ]
    llm, verified = await _drive(tmp_path, monkeypatch, turns)

    assert _reminders(llm) == []
    assert len(verified) == 1
