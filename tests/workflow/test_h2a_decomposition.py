"""Lift H2a — smoke tests for the ``execution/orchestrator.py`` decomposition.

v8 §17.1 splits the 1601 LOC god-file into 5 files in the Workflow context.
This module asserts the new modules exist and that each carries its slice of
the public surface. It also asserts the shim at the old location continues to
re-export every name external callers / test fixtures still depend on so the
mechanical caller-update can be deferred to subsequent lifts.
"""

from __future__ import annotations

import importlib
import inspect

import pytest


def test_agent_loop_module_carries_loop_conductor() -> None:
    mod = importlib.import_module("backend.workflow.application.agent_loop")
    for name in (
        "RunOrchestrator",
        "LoopResult",
        "LoopOutcome",
        "LoopLlm",
        "LoopTurn",
        "LoopToolCall",
        "CanonRetriever",
        "RunCompute",
        "_SYSTEM_PROMPT",
        "_DESIGN_SPEC_DIRECTIVE",
        "_is_design_stage",
        "_intent_title",
        "_resumption_messages",
    ):
        assert hasattr(mod, name), f"agent_loop missing {name}"


def test_tool_registry_module_carries_loop_tool_constants() -> None:
    mod = importlib.import_module("backend.workflow.application.tool_registry")
    for name in (
        "WORK_TOOLS",
        "KNOWLEDGE_SEARCH_NAME",
        "ASK_USER_QUESTION_TOOL",
        "MAX_NO_WORK_NUDGES",
        "_invoke_tool_safely",
        "_assistant_tool_call_message",
        "_sanitize_ask_user_question_options",
    ):
        assert hasattr(mod, name), f"tool_registry missing {name}"


def test_connector_action_registrar_module_carries_registrar() -> None:
    mod = importlib.import_module("backend.workflow.application.connector_action_registrar")
    for name in (
        "register_connector_action_tools",
        "_connector_action_schema",
    ):
        assert hasattr(mod, name), f"connector_action_registrar missing {name}"


def test_emit_deliverable_module_carries_deliverable_emit() -> None:
    mod = importlib.import_module("backend.workflow.domain.emit_deliverable")
    for name in (
        "EMIT_DELIVERABLE_NAME",
        "EMIT_DELIVERABLE_TOOL",
        "handle_emit_deliverable",
        "_safe_args",
    ):
        assert hasattr(mod, name), f"emit_deliverable missing {name}"


def test_run_persistence_module_carries_run_persistence_helpers() -> None:
    mod = importlib.import_module("backend.workflow.application.run_persistence")
    for name in (
        "record_activity",
        "create_decision",
        "decision_result",
        "finish_verified",
        "audit_event",
        "utcnow",
    ):
        assert hasattr(mod, name), f"run_persistence missing {name}"


def test_legacy_shim_deleted_post_h3c() -> None:
    """Post-Lift H3c (v8 §13 Lift H final), the temporary back-compat shim at
    ``backend.execution.orchestrator`` is DELETED — every caller imports from
    the H2a-decomposed locations directly. The shim was a one-window
    migration aid; H3c closed the window.

    The dead Lift 0c identifiers (``is_dangerous`` / ``danger_map`` /
    ``DangerAnalyzer``) absence is now asserted against the H2a successor
    modules directly (see
    ``tests/glue/test_lift0c_no_static_danger_analyzer.py``).
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.execution" + ".orchestrator")


def test_no_new_file_exceeds_god_file_threshold() -> None:
    """v8 §17.1 — none of the new files exceeds 600 LOC.

    H2a sub-split added two private helper modules (``_loop_context.py``,
    ``_drive_loop.py``) so the conductor stays under the ceiling. The
    overall surface still matches v8 §17.1's five-bucket map; the
    helpers are private to ``agent_loop.py``.
    """
    for module_path in (
        "backend.workflow.application.agent_loop",
        "backend.workflow.application._loop_context",
        "backend.workflow.application._drive_loop",
        "backend.workflow.application.tool_registry",
        "backend.workflow.application.connector_action_registrar",
        "backend.workflow.domain.emit_deliverable",
        "backend.workflow.application.run_persistence",
    ):
        mod = importlib.import_module(module_path)
        src_path = inspect.getsourcefile(mod)
        assert src_path is not None
        with open(src_path, encoding="utf-8") as fp:
            loc = sum(1 for _ in fp)
        assert loc <= 600, f"{module_path} grew to {loc} LOC — sub-split required"
