"""Lift H2c — smoke tests for the agent_runner relocation + handler scaffolding.

v8 §13 Lift H2c closure. The legacy ``backend/orchestrator/`` directory
is removed entirely; ``AgentRunner`` lives in the Workflow context; and
the H1 transitions matrix is backed by Protocol-conforming handler
classes invoked through a single ``state_machine_driver``.

This module locks the 7 deltas the lift must prove.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# ─────────────────────── Delta 1 — agent_runner moved ─────────────────────────


def test_agent_runner_new_path_importable() -> None:
    mod = importlib.import_module("backend.workflow.application.agent_runner")
    assert hasattr(mod, "AgentRunner")


def test_agent_runner_old_path_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.orchestrator.agent_runner")


# ─────────────────────── Delta 2 — orchestrator/ dir removed ──────────────────


def test_backend_orchestrator_package_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.orchestrator")


def test_backend_orchestrator_dir_does_not_exist() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    assert not (repo_root / "backend" / "orchestrator").exists()


# ─────────────────────── Delta 3 — handler classes exist ──────────────────────


_HANDLER_NAMES = (
    # Frame stage
    "FrameCompleteHandler",
    "RouteCompleteHandler",
    # Run stage
    "DispatchHandler",
    "RequireDecisionHandler",
    "ResolveDecisionHandler",
    "RetryFailedHandler",
    # Verify stage
    "StartVerifyHandler",
    "VerifyPassHandler",
    "VerifyFailHandler",
    # Settle stage
    "ShipHandler",
    "SettleCompleteHandler",
    # Deliver stage
    "DeliverCompleteHandler",
    # Cross-stage
    "FailHandler",
    "AbandonHandler",
    "ExpireHandler",
)


def test_all_handler_classes_importable_from_handlers_pkg() -> None:
    mod = importlib.import_module("backend.workflow.application._handlers")
    for name in _HANDLER_NAMES:
        assert hasattr(mod, name), f"_handlers pkg missing {name}"


def test_handler_classes_match_transition_matrix() -> None:
    from backend.workflow.application import _handlers as handlers_pkg
    from backend.workflow.domain.transitions import (
        CROSS_STAGE_TRANSITIONS,
        TRANSITION_MATRIX,
    )

    referenced: set[str] = set()
    for entry in TRANSITION_MATRIX.values():
        referenced.add(entry.handler_name)
    for entry in CROSS_STAGE_TRANSITIONS.values():
        referenced.add(entry.handler_name)
    missing = [name for name in referenced if not hasattr(handlers_pkg, name)]
    assert not missing, f"transition matrix references missing handlers: {missing}"


def test_handler_classes_implement_protocol() -> None:
    """Every handler class has an ``async def handle(...)`` coroutine."""
    import inspect

    mod = importlib.import_module("backend.workflow.application._handlers")
    for name in _HANDLER_NAMES:
        cls = getattr(mod, name)
        assert inspect.isclass(cls), f"{name} is not a class"
        assert hasattr(cls, "handle"), f"{name}.handle missing"
        assert inspect.iscoroutinefunction(cls.handle), f"{name}.handle is not async"


# ─────────────────────── Delta 4 — state machine driver ───────────────────────


def test_state_machine_driver_module_importable() -> None:
    mod = importlib.import_module("backend.workflow.application.state_machine_driver")
    assert hasattr(mod, "drive_transition")


@pytest.mark.asyncio
async def test_state_machine_driver_returns_next_state_for_valid_pair() -> None:
    """Driver looks up (state, event) in the matrix and returns the to_state.

    Handler dispatch happens but with no side-effect target wired in (run is a
    mock); the driver still returns the matrix's next state.
    """
    from unittest.mock import MagicMock

    from backend.workflow.application.state_machine_driver import drive_transition
    from backend.workflow.domain.state import WorkflowEvent, WorkflowState

    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.received,
        event=WorkflowEvent.frame_complete,
    )
    assert next_state == WorkflowState.framed


@pytest.mark.asyncio
async def test_state_machine_driver_raises_on_invalid_transition() -> None:
    """Invalid (state, event) pair raises."""
    from unittest.mock import MagicMock

    from backend.workflow.application.state_machine_driver import (
        InvalidTransitionError,
        drive_transition,
    )
    from backend.workflow.domain.state import WorkflowEvent, WorkflowState

    with pytest.raises(InvalidTransitionError):
        await drive_transition(
            run=MagicMock(),
            current_state=WorkflowState.received,
            event=WorkflowEvent.ship,
        )


@pytest.mark.asyncio
async def test_state_machine_driver_routes_cross_stage_events() -> None:
    from unittest.mock import MagicMock

    from backend.workflow.application.state_machine_driver import drive_transition
    from backend.workflow.domain.state import WorkflowEvent, WorkflowState

    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.dispatched,
        event=WorkflowEvent.abandon,
    )
    assert next_state == WorkflowState.abandoned


# ─────────────────────── Delta 5 — no source tree stragglers ──────────────────


def test_no_remaining_backend_orchestrator_imports_in_source() -> None:
    """No file in the source tree should still ``from backend.orchestrator`` import."""
    repo_root = Path(__file__).resolve().parents[2]
    needles = (
        "from backend.orchestrator",
        "import backend.orchestrator",
    )
    offenders: list[str] = []
    for path in repo_root.rglob("*.py"):
        rel = path.relative_to(repo_root)
        if rel.parts and rel.parts[0] in {
            ".venv",
            "venv",
            "node_modules",
            ".git",
            "var",
            "wt",
        }:
            continue
        # Allow this test file (it documents the removed surface).
        if rel == Path("tests/workflow/test_h2c_relocation.py"):
            continue
        # Allow the H2b smoke test (it documents H2b's removed submodules).
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
    assert not offenders, "stale backend.orchestrator imports remain:\n" + "\n".join(offenders)
