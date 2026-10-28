"""A product's quick checks run when the agent says "done" — before a verification round.

Prod #1259 (run 686d3874, 2026-10-28): round 1's ledger, floor and derived checks all passed,
and the round failed at its LAST step — the product's final `mypy` — on a one-line type error.
A 16-minute round was spent to learn what a one-minute check could have said first.

So a product may declare ``verify_precheck``: commands run when the agent finishes, before the
round. A failure goes back to the agent as a turn, not a round — at most a few times per drive,
then verification decides as before.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

import backend.workflow.infrastructure.delivery.db  # noqa: F401 — terminal contract tables
from backend.identity.workspaces_db import ProductRow
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.application.precheck import MAX_PRECHECKS
from backend.workflow.domain.declared_checks import (
    PRECHECK_KEY,
    read_declared_checks,
    validate_declared_checks,
)
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from tests._support import memory_session

_MARK = "QUICK CHECKS FAILED"


def test_the_precheck_is_declared_beside_floor_and_final() -> None:
    assert read_declared_checks({PRECHECK_KEY: ["uv run mypy backend/"]}).precheck == (
        "uv run mypy backend/",
    )
    with pytest.raises(ValueError):
        validate_declared_checks({PRECHECK_KEY: "uv run mypy backend/"})


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


_DECLARE = _tool("declare_verification", checks=[{"kind": "command", "command": "true"}])


async def _drive(
    tmp_path: Path, monkeypatch: Any, turns: list[LoopTurn], metadata: dict[str, Any]
) -> tuple[_ScriptedLlm, list[Any]]:
    repo = _repo(tmp_path / "ws")
    llm = _ScriptedLlm(turns)
    verified: list[Any] = []
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        product = ProductRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            name="P",
            slug=f"p-{uuid.uuid4().hex[:6]}",
            product_metadata=metadata,
        )
        session.add(product)
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=product.id,
            status=RunStatus.RUNNING,
            payload={"intent_text": "x"},
        )
        session.add(run)
        await session.flush()
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())

        async def fake_verify(**kwargs: Any) -> VerificationResult:
            verified.append(kwargs)
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
        str(m["content"])
        for m in llm.seen[-1]
        if m.get("role") == "user" and _MARK in str(m.get("content"))
    ]


async def test_a_failing_quick_check_goes_back_as_a_turn_not_a_round(
    tmp_path: Path, monkeypatch: Any
) -> None:
    turns = [
        LoopTurn(
            content="declare + write",
            tool_calls=(_DECLARE, _tool("file_write", path="a.py", content="x = 1\n")),
        ),
        LoopTurn(content="done", tool_calls=()),
        LoopTurn(
            content="fix",
            tool_calls=(_tool("file_write", path="typed.ok", content="ok\n"),),
        ),
        LoopTurn(content="done", tool_calls=()),
    ]
    llm, verified = await _drive(
        tmp_path,
        monkeypatch,
        turns,
        {
            PRECHECK_KEY: [
                "test -f typed.ok || { echo 'a.py:1: error: incompatible type'; exit 1; }"
            ]
        },
    )

    [reminder] = _reminders(llm)
    assert "a.py:1: error: incompatible type" in reminder
    assert len(verified) == 1  # one round, spent only after the quick check passed


async def test_quick_checks_ask_at_most_a_few_times_then_verification_decides(
    tmp_path: Path, monkeypatch: Any
) -> None:
    turns = [
        LoopTurn(
            content="declare + write",
            tool_calls=(_DECLARE, _tool("file_write", path="a.py", content="x = 1\n")),
        ),
        *[LoopTurn(content="done", tool_calls=()) for _ in range(MAX_PRECHECKS + 1)],
    ]
    llm, verified = await _drive(tmp_path, monkeypatch, turns, {PRECHECK_KEY: ["exit 1"]})

    assert len(_reminders(llm)) == MAX_PRECHECKS
    assert len(verified) == 1


async def test_a_product_without_quick_checks_is_verified_directly(
    tmp_path: Path, monkeypatch: Any
) -> None:
    turns = [
        LoopTurn(
            content="declare + write",
            tool_calls=(_DECLARE, _tool("file_write", path="a.py", content="x = 1\n")),
        ),
        LoopTurn(content="done", tool_calls=()),
    ]
    llm, verified = await _drive(tmp_path, monkeypatch, turns, {})

    assert _reminders(llm) == []
    assert len(verified) == 1


async def test_a_quick_check_runs_under_ci_conditions(tmp_path: Path, monkeypatch: Any) -> None:
    turns = [
        LoopTurn(
            content="declare + write",
            tool_calls=(_DECLARE, _tool("file_write", path="a.py", content="x = 1\n")),
        ),
        LoopTurn(content="done", tool_calls=()),
    ]
    llm, verified = await _drive(
        tmp_path, monkeypatch, turns, {PRECHECK_KEY: ['test "$CI" = true']}
    )
    assert _reminders(llm) == []
    assert len(verified) == 1
