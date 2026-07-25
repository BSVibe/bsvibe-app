"""Lift H2b — regression tripwires for the legacy ``backend.orchestrator`` teardown.

v8 §13 / Class Architecture Design v8 §13 Lift H2b: the legacy 4-stage state
machine (``workflow_sm.py`` + ``schema.py``), the Frame stage
(``frame.py``), and the Safe Mode boundary (``safe_mode.py``) moved out of
``backend/orchestrator/`` into the Workflow bounded context.

The legacy 4-stage state machine and the ``SafeModeBoundary`` stub have since
been deleted outright — the v8 ``WorkflowState`` machine
(:mod:`backend.workflow.domain.state` + :mod:`backend.workflow.domain.transitions`
+ :mod:`backend.workflow.application.state_machine_driver`) fully supersedes
them. What remains worth guarding here are the *structural* invariants:

1. The legacy ``backend.orchestrator.{workflow_sm,schema,frame,safe_mode}``
   modules stay removed (regression tripwire).
2. The v8 enum surface + the absorbed ``FrameStage`` stay reachable at their
   new homes.
3. No file re-introduces an import of the moved-out submodules.
4. The whole ``backend/orchestrator/`` directory stays collapsed (H2c).
"""

from __future__ import annotations

import importlib
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


# ─────────────────────── Delta 2 — v8 surface reachable at new homes ─────────


def test_workflow_state_module_carries_v8_surface() -> None:
    mod = importlib.import_module("backend.workflow.domain.state")
    for name in ("WorkflowState", "WorkflowEvent"):
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


# ─────────────────────── Delta 6 — FrameStage class intact ───────────────────


def test_frame_stage_is_class_with_frame_method() -> None:
    from backend.workflow.application.stages.frame import FrameStage

    assert hasattr(FrameStage, "frame")


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
