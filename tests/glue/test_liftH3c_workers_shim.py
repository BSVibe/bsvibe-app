"""Lift H3c — workers relocation + H2a shim removal.

Per v8 §13 Lift H final + D34 (workers belong in
``<context>/infrastructure/workers/``):

* 6 workflow workers move to ``backend/workflow/infrastructure/workers/``:
  ``agent_worker``, ``verifier_worker``, ``relay_worker``, ``run``,
  ``intake_worker``, ``delivery_worker``.
* ``settle_worker`` moves to ``backend/knowledge/infrastructure/workers/``.
* ``schedule_runner`` stays at ``backend/workers/`` pending the Schedule
  context lift (separate follow-up). ``base``, ``__main__``, ``__init__``,
  ``db``, ``emit``, ``streams``, ``relays`` are common worker infra used
  across contexts and also stay.
* The H2a shim ``backend/execution/orchestrator.py`` is deleted — every
  consumer imports from its H2a-decomposed location directly.

These tests are the RED-first delta proof.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Delta 1: 6 workflow workers moved to backend.workflow.infrastructure.workers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name,symbol",
    [
        ("backend.workflow.infrastructure.workers.agent_worker", "AgentWorker"),
        ("backend.workflow.infrastructure.workers.verifier_worker", "VerifierWorker"),
        ("backend.workflow.infrastructure.workers.relay_worker", "RelayWorker"),
        ("backend.workflow.infrastructure.workers.run", "build_worker_runtime"),
        ("backend.workflow.infrastructure.workers.intake_worker", "IntakeWorker"),
        ("backend.workflow.infrastructure.workers.delivery_worker", "DeliveryWorker"),
    ],
)
def test_workflow_worker_at_new_location(module_name: str, symbol: str) -> None:
    """Workflow workers are importable at ``backend.workflow.infrastructure.workers.*``."""
    mod = importlib.import_module(module_name)
    assert hasattr(mod, symbol), f"{module_name} missing {symbol}"


# ---------------------------------------------------------------------------
# Delta 2: settle_worker moved to backend.knowledge.infrastructure.workers
# ---------------------------------------------------------------------------


def test_settle_worker_at_new_location() -> None:
    """``settle_worker`` lives under Knowledge infrastructure."""
    mod = importlib.import_module("backend.knowledge.infrastructure.workers.settle_worker")
    assert hasattr(mod, "SettleWorker")
    assert hasattr(mod, "KnowledgeSettleSink")


# ---------------------------------------------------------------------------
# Delta 3: OLD per-worker module paths under backend.workers.* are GONE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    [
        # String-concatenated so the bulk import rewriter cannot rewrite
        # them out — the whole point is to assert the old path is gone.
        "backend.workers" + ".agent_worker",
        "backend.workers" + ".verifier_worker",
        "backend.workers" + ".relay_worker",
        "backend.workers" + ".intake_worker",
        "backend.workers" + ".delivery_worker",
        "backend.workers" + ".settle_worker",
        "backend.workers" + ".run",
    ],
)
def test_old_worker_module_gone(module_name: str) -> None:
    """Each relocated worker no longer resolves at its old path."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


# ---------------------------------------------------------------------------
# Delta 4: Common worker infra preserved at backend/workers/
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    [
        "backend.workers",
        "backend.workers.base",
        "backend.workers.__main__",
        "backend.workers.db",
        "backend.workers.emit",
        "backend.workers.streams",
        # ``backend.workers.schedule_runner`` was a carry-over from H3c
        # that the Schedule lift retired — the schedule runner now lives
        # under the Schedule bounded context.
        "backend.workers.relays",
    ],
)
def test_common_worker_infra_preserved(module_name: str) -> None:
    """Common infra (BaseWorker, schemas, streams, schedule_runner) stays."""
    mod = importlib.import_module(module_name)
    assert mod is not None


# ---------------------------------------------------------------------------
# Delta 5: backend/execution/orchestrator.py (H2a shim) deleted
# ---------------------------------------------------------------------------


def test_h2a_shim_module_gone() -> None:
    """``backend.execution.orchestrator`` no longer importable."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.execution" + ".orchestrator")


def test_h2a_shim_file_deleted() -> None:
    """The shim file itself is removed from the source tree.

    Lift I-0 went further and deleted the whole ``backend/execution/`` package;
    asserting against the repo root is the durable form of this check.
    """
    import backend

    backend_root = Path(next(iter(backend.__path__))).parent
    assert not (backend_root / "backend" / "execution" / "orchestrator.py").exists()
    assert not (backend_root / "backend" / "execution").exists()


def test_no_consumer_imports_from_h2a_shim() -> None:
    """No source file in backend/ tests/ apps/ imports from the shim path."""
    import backend

    backend_root = Path(next(iter(backend.__path__))).parent
    needle_from = "from backend.execution" + ".orchestrator"
    needle_import = "import backend.execution" + ".orchestrator"
    offenders: list[str] = []
    for sub in ("backend", "tests"):
        root = backend_root / sub
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            text = py.read_text(encoding="utf-8", errors="ignore")
            if needle_from in text or needle_import in text:
                # Exclude this very test file (its own assertions reference the
                # string, intentionally).
                if py.name == "test_liftH3c_workers_shim.py":
                    continue
                offenders.append(str(py.relative_to(backend_root)))
    assert not offenders, f"Found H2a shim consumers: {offenders}"


# ---------------------------------------------------------------------------
# Delta 6: worker runtime build_worker_runtime still wires
# ---------------------------------------------------------------------------


def test_worker_runtime_module_imports() -> None:
    """``backend.workflow.infrastructure.workers.run`` imports + exposes
    ``build_worker_runtime`` and ``run_workers``."""
    mod = importlib.import_module("backend.workflow.infrastructure.workers.run")
    assert hasattr(mod, "build_worker_runtime")
    assert hasattr(mod, "run_workers")
    assert hasattr(mod, "build_agent_execution_deps")


def test_workers_dunder_main_still_boots() -> None:
    """``python -m backend.workers`` entrypoint still resolves run_workers."""
    mod = importlib.import_module("backend.workers.__main__")
    assert hasattr(mod, "main")


# ---------------------------------------------------------------------------
# Delta 7: backend/workers/__init__.py does not re-export moved workers
# ---------------------------------------------------------------------------


def test_workers_init_does_not_export_moved_workers() -> None:
    """The ``backend.workers`` package no longer re-exports moved workers.

    Tests + production callers must import from the new canonical locations
    (per D34: workers belong in ``<context>/infrastructure/workers/``)."""
    import backend.workers as pkg

    for sym in (
        "AgentWorker",
        "DeliveryWorker",
        "IntakeWorker",
        "RelayWorker",
        "SettleWorker",
        "VerifierWorker",
    ):
        assert not hasattr(pkg, sym), (
            f"backend.workers re-exports moved worker {sym!r} — "
            "delete the re-export to enforce the new structure"
        )
