"""A run that changes nothing between failed verifications is stuck — the founder is asked.

Prod #1204 (run 2538f8bc, 2026-10-26): after resuming, the run failed verification 8 rounds
running and wrote one file in all of them (1.67M tokens). The same thing broke every time — the
sandbox's ``pnpm install`` — but the gate's commands are DERIVED by a model each round and
their text drifted (``cd apps/pwa && npx … install`` / ``npx … install`` / ``… --frozen-lockfile
|| …``), so ``failure_signature`` never saw a repeat and ``REPEAT_LIMIT`` never fired.

What cannot drift is the tree: if the agent's work is byte-identical to what last failed, the
next verdict is not going to be different because of anything the agent did.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

import backend.workflow.infrastructure.delivery.db  # noqa: F401 — terminal contract tables
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.application.verify_stall import (
    NO_PROGRESS,
    REPEAT_LIMIT,
    StallTracker,
    tree_fingerprint,
)
from backend.workflow.infrastructure.db import (
    Decision,
    ExecutionRun,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.sandbox.noop_manager import NoopSandboxSession
from tests._support import memory_session


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _failed(round_no: int) -> dict[str, Any]:
    """A failed verdict whose derived command TEXT drifts every round, as a re-derived gate's
    does, while the failure itself (the install) is the same."""
    command = ["npx --yes pnpm@10 install", "cd apps/pwa && npx --yes pnpm@10 install"][
        round_no % 2
    ]
    return {
        "gate_expected": True,
        "derived_gate": {
            "origin": "derived",
            "applicable": True,
            "passed": False,
            "commands": [
                {
                    "command": command,
                    "kind": "quality",
                    "status": "failed",
                    "exit_code": 1,
                    "output": f"ERR_PNPM_IGNORED_BUILDS (round {round_no})",
                }
            ],
        },
    }


# ── the tracker ──────────────────────────────────────────────────────────────


def test_an_unchanged_tree_counts_as_a_repeat_even_when_the_failure_text_drifts() -> None:
    tracker = StallTracker()
    for n in range(REPEAT_LIMIT + 1):
        tracker.observe(_failed(n), tree="same-tree")
    assert tracker.repeats == REPEAT_LIMIT
    assert tracker.unchanged is True


def test_a_changing_tree_with_a_drifting_failure_is_not_a_repeat() -> None:
    tracker = StallTracker()
    for n in range(REPEAT_LIMIT + 2):
        tracker.observe(_failed(n), tree=f"tree-{n}")
    assert tracker.repeats == 0


def test_an_unknown_tree_falls_back_to_the_failure_signature() -> None:
    tracker = StallTracker()
    same = _failed(0)
    for _ in range(REPEAT_LIMIT + 1):
        tracker.observe(same, tree=None)
    assert tracker.repeats == REPEAT_LIMIT
    assert tracker.unchanged is False  # the signature, not the tree, said so


async def test_the_fingerprint_moves_with_the_work_and_is_none_outside_git(tmp_path: Path) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    assert await tree_fingerprint(NoopSandboxSession(str(outside)), None) is None

    repo = _repo(tmp_path / "repo")
    box = NoopSandboxSession(str(repo))
    clean = await tree_fingerprint(box, None)
    assert clean == await tree_fingerprint(box, None)
    (repo / "README.md").write_text("changed\n")
    edited = await tree_fingerprint(box, None)
    (repo / "new.py").write_text("x = 1\n")
    added = await tree_fingerprint(box, None)
    assert len({clean, edited, added}) == 3


# ── through the drive loop ───────────────────────────────────────────────────


class _ScriptedLlm:
    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)

    async def complete(self, *, messages: list[dict[str, Any]], tools: Any) -> LoopTurn:
        if not self._turns:
            raise AssertionError("scripted LLM exhausted — the loop kept going")
        return self._turns.pop(0)


def _tool(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


async def test_a_run_that_stops_changing_its_work_is_asked_about_not_driven_on(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo = _repo(tmp_path / "ws")
    turns = [
        LoopTurn(
            content="declare + write",
            tool_calls=(
                _tool("declare_verification", checks=[{"kind": "command", "command": "true"}]),
                _tool("file_write", path="a.py", content="x = 1\n"),
            ),
        ),
        # Every later round: the agent says it is done and changes nothing.
        *[LoopTurn(content="done", tool_calls=()) for _ in range(12)],
    ]
    async with memory_session() as session:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            status=RunStatus.RUNNING,
            payload={"intent_text": "x"},
        )
        session.add(run)
        await session.flush()
        orch = RunOrchestrator(
            session=session, llm=_ScriptedLlm(turns), sandbox_manager=NoopSandboxManager()
        )
        rounds: list[int] = []

        async def fake_verify(**kwargs: Any) -> VerificationResult:
            rounds.append(len(rounds))
            vr = VerificationResult(
                id=uuid.uuid4(),
                run_id=run.id,
                work_step_id=kwargs["work_step"].id,
                workspace_id=run.workspace_id,
                outcome=VerificationOutcome.FAILED,
                contract={},
                result=_failed(len(rounds)),
            )
            session.add(vr)
            await session.flush()
            return vr

        monkeypatch.setattr(orch, "_verify", fake_verify)
        await orch.run(run=run, workspace_dir=repo)

        decisions = (await session.execute(select(Decision))).scalars().all()
    reasons = [d.payload.get("reason") for d in decisions]
    assert NO_PROGRESS in reasons, reasons
    # first failure + REPEAT_LIMIT repeats, then the question — not the whole round budget
    assert len(rounds) == REPEAT_LIMIT + 2
