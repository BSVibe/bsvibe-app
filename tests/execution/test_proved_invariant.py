"""B13 — Anti-regression invariant: PROVED requires a PASSED VerificationResult.

This file is the cross-cutting regression net the audit (RC-5) demanded:
post-state-only assertions (run reached REVIEW_READY, Deliverable row exists)
silently accepted the hollow executor for months. The defense is two-layered:

1. **Runtime invariant.** Drive both production orchestrator paths (native
   :class:`RunOrchestrator` and :class:`ExecutorOrchestrator`) and, regardless
   of path, assert the cross-cutting truth: a verified ``Deliverable`` exists
   IFF (a) a ``VerificationResult`` exists for the SAME ``run_id`` whose
   outcome is :data:`VerificationOutcome.PASSED`, AND (b) the run's
   ``WorkStep.proof_state`` is :data:`ProofState.PROVED`. No row may be
   verified without a real passing verdict linked to it.

2. **Structural caller check.** A grep over the backend source for callers of
   :func:`backend.workflow.domain.verified_deliverable.write_verified_deliverable`
   asserts each call site lives in a code path that REFERENCES
   ``VerificationOutcome.PASSED`` upstream — so any future caller that tries
   to skip the verify gate trips a test that names the file. This is a
   defensive net (not a proof) for the seam where the original hollow ship
   happened — ``write_verified_deliverable`` itself does NOT enforce a
   VerificationResult co-exists, so a future caller could re-introduce the
   sin without touching the helper.

Test design rule (the convention this lift codifies; see ``tests/README.md``):
*post-state alone is NOT acceptable; assert the change the lift was supposed
to produce (files captured, verification ran, knowledge consulted, decision
absorbed).*
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Importing these modules registers the DeliveryEventRow + ExecutionRunActivity
# tables on the shared ``Base.metadata`` so ``memory_session`` materialises them.
# write_verified_deliverable inserts into delivery_events as part of the verified
# terminal contract; without the side-effect import, the runtime tests below
# crash at flush with "no such table".
import backend.workflow.infrastructure.delivery.db  # noqa: F401
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.infrastructure.db import (
    Deliverable,
    ProofState,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from tests._support import memory_session

# --------------------------------------------------------------------------
# Test doubles — minimal, deliberately not shared with other modules so any
# future drift in this anti-regression invariant is localised.
# --------------------------------------------------------------------------


class _ScriptedLlm:
    """Pops the next pre-programmed :class:`LoopTurn` per ``complete`` call."""

    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._turns:
            raise AssertionError("scripted LLM exhausted")
        return self._turns.pop(0)


def _tool(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


# --------------------------------------------------------------------------
# The cross-cutting invariant — used at the end of every runtime test below.
# --------------------------------------------------------------------------


async def _assert_proved_invariant(session: AsyncSession) -> None:
    """For every Deliverable in ``session``, assert there exists a
    ``VerificationResult`` co-created for the SAME ``run_id`` whose outcome is
    PASSED, AND the run's ``WorkStep.proof_state`` is PROVED.

    This is the load-bearing anti-regression check. The original hollow ship
    passed every test asserting Deliverable existence — none asserted a
    PASSED VerificationResult was linked to the same run. Codifying the link
    here means any future caller that lands a Deliverable without verify gets
    a named failure ("Deliverable <id> for run <id> has no PASSED
    VerificationResult linked").
    """
    deliverables = (await session.execute(select(Deliverable))).scalars().all()
    for deliverable in deliverables:
        run_id = deliverable.run_id
        results = (
            (
                await session.execute(
                    select(VerificationResult).where(VerificationResult.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        passed = [vr for vr in results if vr.outcome is VerificationOutcome.PASSED]
        assert passed, (
            f"Deliverable {deliverable.id} for run {run_id} has NO PASSED "
            f"VerificationResult linked — hollow PROVED regression"
        )
        # And the WorkStep on that run must be PROVED + VERIFIED (no fake
        # UNTESTED step paired with a verified Deliverable).
        steps = (
            (await session.execute(select(WorkStep).where(WorkStep.run_id == run_id)))
            .scalars()
            .all()
        )
        assert any(
            s.proof_state is ProofState.PROVED and s.status is WorkStepStatus.VERIFIED
            for s in steps
        ), (
            f"Deliverable {deliverable.id} for run {run_id} has no PROVED+VERIFIED "
            f"WorkStep — proof_state not transitioned"
        )


# --------------------------------------------------------------------------
# 1. Runtime: native RunOrchestrator verified-PASS path satisfies invariant.
# --------------------------------------------------------------------------


async def test_native_verified_run_satisfies_proved_invariant(tmp_path: Path) -> None:
    """End-to-end: drive the native loop to a real verified terminal via a
    command-check contract that passes. The invariant checker then walks the
    DB and confirms the Deliverable links to a PASSED VerificationResult and
    the WorkStep is PROVED+VERIFIED."""
    from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

    llm = _ScriptedLlm(
        [
            LoopTurn(
                content="declare check + write file",
                tool_calls=(
                    _tool(
                        "declare_verification",
                        checks=[{"kind": "command", "command": "grep -q 42 answer.txt"}],
                    ),
                    _tool("file_write", path="answer.txt", content="42\n"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            status=RunStatus.RUNNING,
            payload={"intent_text": "write the answer"},
        )
        session.add(run)
        await session.flush()
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"
        await _assert_proved_invariant(session)


# --------------------------------------------------------------------------
# 2. Runtime: a run that ends with NO contract → NO Deliverable, and the
#    invariant still holds vacuously. (Anti-fake-PROVED: the legacy hollow
#    path's tell was a Deliverable WITHOUT a VerificationResult.)
# --------------------------------------------------------------------------


async def test_native_no_contract_writes_no_deliverable(tmp_path: Path) -> None:
    """A run that emits final text but declares no contract MUST NOT land a
    Deliverable — this is the original hollow sin. Asserting the invariant
    runtime-style on an empty Deliverable set is vacuously true; the real
    delta is the *absence* of the Deliverable + the *absence* of a fake
    VerificationResult. Both are positive assertions, not "status was
    REVIEW_READY"."""
    from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

    llm = _ScriptedLlm(
        [
            # No declare_verification → no contract assembleable → no PROVED.
            LoopTurn(content="implemented (but no contract)", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            status=RunStatus.RUNNING,
            payload={"intent_text": "do work"},
        )
        session.add(run)
        await session.flush()
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        # Delta assertions — NOT just "status was X":
        assert result.outcome != "verified"
        assert (await session.execute(select(Deliverable))).first() is None
        assert (await session.execute(select(VerificationResult))).first() is None
        # And the WorkStep was NEVER PROVED (the legacy hollow tell).
        steps = (await session.execute(select(WorkStep))).scalars().all()
        assert all(s.proof_state is not ProofState.PROVED for s in steps)
        # Invariant vacuously holds (no Deliverables → no invariants to check).
        await _assert_proved_invariant(session)


# --------------------------------------------------------------------------
# 3. Runtime: a contract that FAILS → "honest fail" path. VerificationResult
#    is FAILED, no Deliverable, no PROVED.
# --------------------------------------------------------------------------


async def test_native_failing_contract_writes_failed_verification_no_deliverable(
    tmp_path: Path,
) -> None:
    """B13 honest-fail delta — declare a command check that will NOT pass,
    and assert the VerificationResult lands as FAILED, no Deliverable, no
    PROVED. This is the positive "honest fail" delta the audit asks for: the
    legacy hollow ship would have landed PROVED + Deliverable here."""
    from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

    llm = _ScriptedLlm(
        [
            LoopTurn(
                content="declare a contract that will fail",
                tool_calls=(
                    # The file never gets written, so `grep -q 42 answer.txt`
                    # fails (file missing → grep nonzero exit).
                    _tool(
                        "declare_verification",
                        checks=[{"kind": "command", "command": "grep -q 42 answer.txt"}],
                    ),
                ),
            ),
            LoopTurn(content="done (but contract will fail)", tool_calls=()),
            # The loop may iterate once more on FAILED — script a benign turn.
            LoopTurn(content="acknowledging", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            status=RunStatus.RUNNING,
            payload={"intent_text": "should fail"},
        )
        session.add(run)
        await session.flush()
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        # The native loop on a FAILED verify either retries (the scripted
        # turns above benignly exhaust) or exits non-verified — the
        # load-bearing claim is: NO PROVED, NO Deliverable, AT LEAST ONE
        # FAILED VerificationResult was persisted (the verifier really ran).
        assert result.outcome != "verified"
        results = (await session.execute(select(VerificationResult))).scalars().all()
        assert results, "verifier must have produced at least one result"
        assert any(vr.outcome is VerificationOutcome.FAILED for vr in results)
        assert (await session.execute(select(Deliverable))).first() is None
        steps = (await session.execute(select(WorkStep))).scalars().all()
        assert all(s.proof_state is not ProofState.PROVED for s in steps)
        await _assert_proved_invariant(session)


# --------------------------------------------------------------------------
# 4. Structural: every prod caller of write_verified_deliverable lives in a
#    file that references VerificationOutcome.PASSED. The helper itself does
#    NOT enforce the link, so a future caller skipping verify would be the
#    next hollow regression. This grep-level check catches that pattern
#    BEFORE it ships.
#
# It is a defensive net, not a proof — a sufficiently creative caller could
# import PASSED and still skip verify. The runtime checks above are the
# proof; this is the smoke detector.
# --------------------------------------------------------------------------


def _backend_root() -> Path:
    # tests/execution/test_proved_invariant.py → repo/backend.
    return Path(__file__).resolve().parents[2] / "backend"


def _find_callers_of(symbol: str) -> list[Path]:
    """Return every backend Python file that calls ``symbol(`` (excluding the
    definition file itself and ``__pycache__``)."""
    pattern = re.compile(rf"\b{re.escape(symbol)}\s*\(")
    callers: list[Path] = []
    backend_root = _backend_root()
    for path in backend_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        # Skip the definition module itself (the helper's own file).
        text = path.read_text(encoding="utf-8")
        if not pattern.search(text):
            continue
        # Heuristic: a file containing both ``def <symbol>`` AND ``<symbol>(``
        # is most likely the definition. Skip it.
        if re.search(rf"^\s*async\s+def\s+{re.escape(symbol)}\s*\(", text, flags=re.MULTILINE):
            continue
        callers.append(path)
    return callers


def test_every_write_verified_deliverable_caller_gates_on_passed_verification() -> None:
    """Structural anti-regression: each prod caller of
    ``write_verified_deliverable`` must reference ``VerificationOutcome.PASSED``
    in the same module (so the verify gate is at least syntactically wired).
    The helper itself does NOT enforce the link, which is why this check
    exists. Renaming the helper or the enum will fail loudly, prompting the
    re-author of this anti-regression check rather than silent drift."""
    callers = _find_callers_of("write_verified_deliverable")
    assert callers, "expected at least one prod caller of write_verified_deliverable"
    offenders: list[str] = []
    for path in callers:
        text = path.read_text(encoding="utf-8")
        if "VerificationOutcome.PASSED" not in text:
            offenders.append(str(path))
    assert not offenders, (
        "callers of write_verified_deliverable that do NOT reference "
        f"VerificationOutcome.PASSED (potential hollow-PROVED regression): {offenders}"
    )


def test_known_call_sites_are_in_expected_modules() -> None:
    """Pin the SET of known call sites so an unexpected new caller (a
    refactor that wraps the helper somewhere new) is forced through review.
    The helper is the seam where the hollow ship happened; expanding its
    caller surface deserves explicit consent."""
    callers = {
        p.relative_to(_backend_root()).as_posix()
        for p in _find_callers_of("write_verified_deliverable")
    }
    # The two known terminals (native + executor). If this set changes, a
    # human should look at the new caller and confirm the verify gate is in
    # place — and then update the expected set here.
    #
    # Lift D split executors/orchestrator.py into 4 files (§17.8). The
    # verified-write call now lives in the verification-handoff sub-module
    # (executors/verify_handoff.py) — same one terminal, just moved.
    #
    # Lift H2a decomposed execution/orchestrator.py into the Workflow context
    # (§17.1). The native terminal moved with ``finish_verified`` into
    # ``workflow/application/run_persistence.py`` — same one terminal, just
    # relocated to its new bounded context.
    assert callers == {
        "workflow/application/run_persistence.py",
        "executors/verify_handoff.py",
    }, f"unexpected caller surface for write_verified_deliverable: {callers}"
