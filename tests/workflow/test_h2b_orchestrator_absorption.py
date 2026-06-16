"""Lift H2b — smoke tests for the legacy ``backend.orchestrator`` absorption.

v8 §13 / Class Architecture Design v8 §13 Lift H2b: the legacy 4-stage state
machine (``workflow_sm.py`` + ``schema.py``), the Frame stage
(``frame.py``), and the Safe Mode boundary (``safe_mode.py``) move out of
``backend/orchestrator/`` into the Workflow bounded context.

This module locks the 6 deltas the lift must prove:

1. Legacy modules removed.
2. Absorbed content reachable at the new locations.
3. No remaining importers of the moved-out submodules.
4. Legacy-projection helper ``to_legacy_stage`` maps the v8 enum to the old
   4-stage Literal.
5. The dataclass ``LegacyWorkflowState`` + ``LegacyWorkflowStateMachine``
   preserve the transition table the old module enforced.
6. The ``FrameStage`` / ``SafeModeBoundary`` classes are unchanged at the
   new locations.

``agent_runner.py`` stays in ``backend/orchestrator/`` for H2c — that is
the *only* legitimate remaining submodule.
"""

from __future__ import annotations

import importlib
import uuid
from pathlib import Path

import pytest

# ─────────────────────── Delta 1 — legacy modules removed ────────────────────


def test_legacy_workflow_sm_module_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.orchestrator.workflow_sm")


def test_legacy_schema_module_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.orchestrator.schema")


def test_legacy_frame_module_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.orchestrator.frame")


def test_legacy_safe_mode_module_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.orchestrator.safe_mode")


# ─────────────────────── Delta 2 — content reachable at new homes ────────────


def test_workflow_state_module_carries_legacy_state_machine() -> None:
    mod = importlib.import_module("backend.workflow.domain.state")
    for name in (
        "LegacyStage",
        "LegacyWorkflowState",
        "LegacyWorkflowStateMachine",
        "InvalidLegacyTransitionError",
        "to_legacy_stage",
        # the v8 surface is preserved
        "WorkflowState",
        "WorkflowEvent",
    ):
        assert hasattr(mod, name), f"workflow.domain.state missing {name}"


def test_workflow_application_stages_frame_present() -> None:
    mod = importlib.import_module("backend.workflow.application.stages.frame")
    for name in (
        "FrameStage",
        "FrameConfig",
        "FrameLlm",
        "FramedRequest",
        "PathClassification",
        "PipelineKind",
    ):
        assert hasattr(mod, name), f"stages.frame missing {name}"


def test_workflow_application_safe_mode_present() -> None:
    mod = importlib.import_module("backend.workflow.application.safe_mode")
    assert hasattr(mod, "SafeModeBoundary"), "safe_mode missing SafeModeBoundary"


# ─────────────────────── Delta 3 — no stragglers in source tree ──────────────


def test_no_legacy_orchestrator_submodule_imports_remain() -> None:
    """No file in the source tree should still ``from
    backend.orchestrator.{workflow_sm,schema,frame,safe_mode}`` import."""
    repo_root = Path(__file__).resolve().parents[2]
    needles = (
        "from backend.orchestrator.workflow_sm",
        "from backend.orchestrator.schema",
        "from backend.orchestrator.frame",
        "from backend.orchestrator.safe_mode",
    )
    offenders: list[str] = []
    for path in repo_root.rglob("*.py"):
        # Skip the test file itself + venv dirs.
        rel = path.relative_to(repo_root)
        if rel.parts and rel.parts[0] in {".venv", "venv", "node_modules", ".git", "wt"}:
            continue
        if rel == Path("tests/workflow/test_h2b_orchestrator_absorption.py"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for needle in needles:
            if needle in text:
                offenders.append(f"{rel}: {needle}")
                break
    assert not offenders, "legacy orchestrator imports still present:\n" + "\n".join(offenders)


# ─────────────────────── Delta 4 — to_legacy_stage projection ────────────────


def test_to_legacy_stage_received_to_receive() -> None:
    from backend.workflow.domain.state import WorkflowState, to_legacy_stage

    assert to_legacy_stage(WorkflowState.received) == "receive"


def test_to_legacy_stage_framed_to_frame() -> None:
    from backend.workflow.domain.state import WorkflowState, to_legacy_stage

    assert to_legacy_stage(WorkflowState.framed) == "frame"


def test_to_legacy_stage_run_states_to_agent_loop() -> None:
    from backend.workflow.domain.state import WorkflowState, to_legacy_stage

    for state in (
        WorkflowState.routed,
        WorkflowState.dispatched,
        WorkflowState.needs_decision,
        WorkflowState.verifying,
        WorkflowState.verified,
        WorkflowState.shipped,
    ):
        assert to_legacy_stage(state) == "agent_loop", state


def test_to_legacy_stage_terminal_states_to_epsilon() -> None:
    from backend.workflow.domain.state import WorkflowState, to_legacy_stage

    for state in (
        WorkflowState.settled,
        WorkflowState.delivered,
        WorkflowState.failed,
        WorkflowState.abandoned,
        WorkflowState.expired,
    ):
        assert to_legacy_stage(state) == "epsilon", state


# ─────────────────────── Delta 5 — legacy SM transitions preserved ───────────


@pytest.mark.asyncio
async def test_legacy_sm_receive_to_frame() -> None:
    from backend.workflow.domain.state import (
        LegacyWorkflowState,
        LegacyWorkflowStateMachine,
    )

    sm = LegacyWorkflowStateMachine()
    state = LegacyWorkflowState(stage="receive", request_id=uuid.uuid4(), run_id=None)
    nxt = await sm.transition(state=state, event="framed")
    assert nxt.stage == "frame"


@pytest.mark.asyncio
async def test_legacy_sm_run_id_preserved() -> None:
    from backend.workflow.domain.state import (
        LegacyWorkflowState,
        LegacyWorkflowStateMachine,
    )

    sm = LegacyWorkflowStateMachine()
    rid = uuid.uuid4()
    state = LegacyWorkflowState(stage="agent_loop", request_id=uuid.uuid4(), run_id=rid)
    nxt = await sm.transition(state=state, event="settled")
    assert nxt.run_id == rid
    assert nxt.stage == "epsilon"


@pytest.mark.asyncio
async def test_legacy_sm_illegal_transition_raises() -> None:
    from backend.workflow.domain.state import (
        InvalidLegacyTransitionError,
        LegacyWorkflowState,
        LegacyWorkflowStateMachine,
    )

    sm = LegacyWorkflowStateMachine()
    state = LegacyWorkflowState(stage="receive", request_id=uuid.uuid4(), run_id=None)
    with pytest.raises(InvalidLegacyTransitionError):
        await sm.transition(state=state, event="settled")


# ─────────────────────── Delta 6 — FrameStage / SafeMode classes intact ──────


def test_frame_stage_is_class_with_frame_method() -> None:
    from backend.workflow.application.stages.frame import FrameStage

    assert hasattr(FrameStage, "frame")


def test_safe_mode_boundary_is_class_with_gate_method() -> None:
    from backend.workflow.application.safe_mode import SafeModeBoundary

    assert hasattr(SafeModeBoundary, "gate")


# ─────────────────────── Bonus: orchestrator/ collapsed by H2c ───────────────


def test_backend_orchestrator_dir_collapsed_by_h2c() -> None:
    """H2c (subsequent lift) collapses the entire ``backend/orchestrator/``
    directory — ``agent_runner.py`` moves to
    ``backend/workflow/application/agent_runner.py``. Asserting absence here
    keeps H2b + H2c in lock-step: if a future lift accidentally re-creates
    the directory, both this test and the H2c relocation test fail."""
    repo_root = Path(__file__).resolve().parents[2]
    orchestrator_dir = repo_root / "backend" / "orchestrator"
    assert not orchestrator_dir.exists(), orchestrator_dir
