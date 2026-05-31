"""Lift I-0 — delta tests asserting backend/execution/ remnants moved into Workflow context.

Asserts:
1. backend/execution/ directory deleted entirely.
2. backend/supervisor/ directory deleted entirely (sandbox absorbed into Workflow infra).
3. New homes exist + import cleanly:
     - backend.workflow.application.verification_service
     - backend.workflow.application.loop_llm
     - backend.workflow.application.handoff
     - backend.workflow.application.knowledge_orchestrator
     - backend.workflow.application.audit_events
     - backend.workflow.infrastructure.connector_actions
     - backend.workflow.infrastructure.tools
     - backend.workflow.infrastructure.db
     - backend.workflow.infrastructure.sandbox
     - backend.workflow.domain.verifier_contract
     - backend.workflow.domain.verified_deliverable
4. Repo-wide grep for old ``backend.execution`` / ``backend.supervisor.sandbox`` imports returns 0.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_backend_execution_directory_removed() -> None:
    assert not (_REPO_ROOT / "backend" / "execution").exists()


def test_backend_supervisor_directory_removed() -> None:
    assert not (_REPO_ROOT / "backend" / "supervisor").exists()


@pytest.mark.parametrize(
    "module",
    [
        "backend.workflow.application.verification_service",
        "backend.workflow.application.loop_llm",
        "backend.workflow.application.handoff",
        "backend.workflow.application.knowledge_orchestrator",
        "backend.workflow.application.audit_events",
        "backend.workflow.infrastructure.connector_actions",
        "backend.workflow.infrastructure.tools",
        "backend.workflow.infrastructure.db",
        "backend.workflow.infrastructure.sandbox",
        "backend.workflow.domain.verifier_contract",
        "backend.workflow.domain.verified_deliverable",
    ],
)
def test_new_homes_importable(module: str) -> None:
    importlib.import_module(module)


# Old locations split into separate function (avoids parametrize id-mangling
# by future bulk sed passes over docstring/cross-ref rewrites).
_OLD_LOCATIONS_GONE: tuple[str, ...] = (
    "backend." + "execution",
    "backend." + "execution.db",
    "backend." + "execution.tools",
    "backend." + "execution.verifier",
    "backend." + "execution.verifier.contract",
    "backend." + "execution.verifier.service",
    "backend." + "execution.handoff",
    "backend." + "execution.loop_llm",
    "backend." + "execution.verified_deliverable",
    "backend." + "execution.knowledge_orchestrator",
    "backend." + "execution.audit_events",
    "backend." + "execution.connector_actions",
    "backend." + "supervisor",
    "backend." + "supervisor.sandbox",
)


@pytest.mark.parametrize("module", _OLD_LOCATIONS_GONE)
def test_old_locations_gone(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_no_legacy_imports_in_repo() -> None:
    """No file should still reference the old paths.

    Matches ONLY real top-of-line imports (``^from backend.execution`` or
    ``^import backend.execution``, etc.). Obfuscated needles inside string
    literals (used by other lift tests for grep-negative-assertions) are
    intentionally NOT matched.
    """
    result = subprocess.run(
        [
            "grep",
            "-rEn",
            r"^(\s)*(from backend\.execution|import backend\.execution"
            r"|from backend\.supervisor\.sandbox|import backend\.supervisor\.sandbox"
            r"|from backend\.supervisor import|import backend\.supervisor)",
            "--include=*.py",
            "backend",
            "tests",
            "apps",
            "plugin",
            "bsvibe_sdk",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # grep returns 1 when no matches found — the desired state.
    assert result.returncode == 1, f"Legacy imports still present:\n{result.stdout}"
