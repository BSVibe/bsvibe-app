"""What a verification left behind is recorded on that verification, not only logged (#1270).

The ``verification_leftovers_excluded`` log line dies with the worker container on every deploy,
so the evidence for whether a stack needs a cache-path list never accumulated. The count and the
top directories now ride the verification's own ``VerificationResult.result``.

Every case runs real git in a temporary repository through ``RunOrchestrator._verify``.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

import backend.workflow.infrastructure.delivery.db  # noqa: F401 — terminal contract tables
from backend.workflow.application.agent_loop import RunOrchestrator
from backend.workflow.domain.verifier_contract import VerificationContract
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    RunAttempt,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.sandbox.noop_manager import NoopSandboxSession
from tests._support import memory_session


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    (root / "src.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return root


class _Verifier:
    """Stands in for the model-bound part; the 'command' writes into the checkout."""

    def __init__(self, root: Path, leave: bool) -> None:
        self._root, self._leave = root, leave

    async def verify(self, **kwargs: Any) -> VerificationResult:
        if self._leave:
            for i in range(3):
                d = self._root / "tool-cache" / "a" / f"b{i}"
                d.mkdir(parents=True)
                (d / "blob").write_text("cached\n")
            (self._root / "probe.log").write_text("out\n")
        run = kwargs["run"]
        return VerificationResult(
            id=uuid.uuid4(),
            run_id=run.id,
            work_step_id=kwargs["work_step"].id,
            workspace_id=run.workspace_id,
            outcome=VerificationOutcome.PASSED,
            contract={},
            result={"command_results": []},
        )


async def _verify(root: Path, *, leave: bool) -> VerificationResult:
    async with memory_session() as session:
        run = ExecutionRun(
            id=uuid.uuid4(), workspace_id=uuid.uuid4(), status=RunStatus.RUNNING, payload={}
        )
        orch = RunOrchestrator(session=session, llm=None, sandbox_manager=NoopSandboxManager())  # type: ignore[arg-type]
        orch._verifier = lambda: _Verifier(root, leave)  # type: ignore[method-assign,assignment,return-value]
        return await orch._verify(
            run=run,
            work_step=WorkStep(id=uuid.uuid4()),
            attempt=RunAttempt(id=uuid.uuid4()),
            contract=VerificationContract(checks=[]),
            box=NoopSandboxSession(str(root)),
            written_paths=[],
            final_text="done",
        )


async def test_a_verification_records_what_it_left_behind(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    vr = await _verify(root, leave=True)

    recorded = vr.result["leftovers_excluded"]
    assert recorded["count"] == 2  # tool-cache/ (one directory entry) and probe.log
    assert {"path": "tool-cache", "count": 1} in recorded["dirs"]
    assert len(recorded["dirs"]) <= 5
    assert vr.result["command_results"] == []  # the rest of the record is untouched


async def test_a_verification_that_leaves_nothing_records_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    vr = await _verify(root, leave=False)

    assert "leftovers_excluded" not in vr.result
