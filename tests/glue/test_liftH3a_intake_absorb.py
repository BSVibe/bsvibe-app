"""Lift H3a — assert ``backend/intake/`` absorbed into Workflow context.

Per v8 §13 Lift H3a + D29 (Intake + Delivery absorption into Workflow), every
file under ``backend/intake/`` moves to a target inside ``backend/workflow/``:

* ``schema.py``       → ``backend/workflow/domain/incoming.py``
* ``db.py``           → ``backend/workflow/infrastructure/intake/db.py``
* ``idempotency.py``  → ``backend/workflow/infrastructure/idempotency.py``
* ``receive.py``      → ``backend/workflow/application/stages/intake.py``
* ``direct.py``       → ``backend/workflow/application/intake/direct.py``
* ``webhook.py``      → ``backend/workflow/application/intake/webhook.py``
* ``decision_resolution.py`` → ``backend/workflow/application/intake/decision_resolution.py``

The two **Schedule context** files (``schedule.py`` + ``schedule_db.py``)
are deliberately left in ``backend/intake/`` — they belong to the future
``backend/schedule/`` M1 context (v8 §3.5 / D30) and will be lifted then.

These tests are the delta-asserting RED-first proof. They check:

1. New canonical Workflow-side imports work.
2. Old ``backend.intake.{schema,db,idempotency,receive,direct,webhook,decision_resolution}``
   imports FAIL (ModuleNotFoundError).
3. ``backend/intake/__init__.py`` no longer re-exports the moved symbols.
4. ``backend/intake/`` directory survives ONLY with the Schedule files.
5. Workers + endpoints still wire (imports resolve, function present).
6. REST endpoints (``api/v1/messages.py`` + ``api/webhooks.py``) stay 4-step
   adapter (D35 strict).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Delta 1: NEW canonical Workflow-side imports work
# ---------------------------------------------------------------------------


def test_workflow_domain_incoming_importable() -> None:
    """``backend.workflow.domain.incoming`` exposes TriggerEvent + literals."""
    mod = importlib.import_module("backend.workflow.domain.incoming")
    assert hasattr(mod, "TriggerEvent")
    assert hasattr(mod, "TriggerKindLiteral")
    assert hasattr(mod, "ActorLiteral")


def test_workflow_infrastructure_intake_db_importable() -> None:
    """SQLAlchemy tables live under ``workflow.infrastructure.intake.db``."""
    mod = importlib.import_module("backend.workflow.infrastructure.intake.db")
    for sym in ("IntakeBase", "TriggerEventRow", "RequestRow", "TriggerKind", "RequestStatus"):
        assert hasattr(mod, sym), sym


def test_workflow_infrastructure_idempotency_importable() -> None:
    mod = importlib.import_module("backend.workflow.infrastructure.idempotency")
    assert hasattr(mod, "is_duplicate")
    assert hasattr(mod, "record")


def test_workflow_application_stages_intake_importable() -> None:
    """Receive stage = the H3a application entry-point for the stage."""
    mod = importlib.import_module("backend.workflow.application.stages.intake")
    for sym in ("receive", "ReceiveOutcome", "filtered_out_record", "RECEIVE_FILTERED_KEY"):
        assert hasattr(mod, sym), sym


def test_workflow_application_intake_trigger_services_importable() -> None:
    """DirectTrigger / WebhookReceiver / DecisionResolutionTrigger move too."""
    direct_mod = importlib.import_module("backend.workflow.application.intake.direct")
    webhook_mod = importlib.import_module("backend.workflow.application.intake.webhook")
    decres_mod = importlib.import_module("backend.workflow.application.intake.decision_resolution")
    assert hasattr(direct_mod, "DirectTrigger")
    assert hasattr(webhook_mod, "WebhookReceiver")
    assert hasattr(webhook_mod, "WebhookOutcome")
    assert hasattr(decres_mod, "DecisionResolutionTrigger")


# ---------------------------------------------------------------------------
# Delta 2: OLD intake module paths are GONE for the absorbed files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    [
        "backend.intake.schema",
        "backend.intake.db",
        "backend.intake.idempotency",
        "backend.intake.receive",
        "backend.intake.direct",
        "backend.intake.webhook",
        "backend.intake.decision_resolution",
    ],
)
def test_old_intake_module_gone(module_name: str) -> None:
    """Absorbed modules can no longer be imported under the old path."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


# ---------------------------------------------------------------------------
# Delta 3: ``backend/intake/__init__.py`` no longer re-exports moved symbols
# ---------------------------------------------------------------------------


def test_backend_intake_init_does_not_reexport_moved_symbols() -> None:
    """The intake package is now entirely gone — Schedule lift moved the
    last two carry-over files to ``backend/schedule/`` and deleted the
    ``backend/intake/`` package.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.intake")


# ---------------------------------------------------------------------------
# Delta 4: ``backend/intake/`` directory: entirely removed
# ---------------------------------------------------------------------------


def test_intake_directory_only_holds_schedule_files() -> None:
    """``backend/intake/`` no longer exists — the Schedule lift moved the
    final two files (``schedule.py`` + ``schedule_db.py``) to the new
    ``backend/schedule/`` bounded context and the now-empty package was
    deleted.
    """
    import backend

    backend_root = Path(next(iter(backend.__path__)))
    assert not (backend_root / "intake").exists(), (
        "backend/intake/ should be gone after the Schedule lift"
    )


# ---------------------------------------------------------------------------
# Delta 5: workers / endpoints / cross-context callers still wire
# ---------------------------------------------------------------------------


def test_intake_worker_imports_resolve() -> None:
    """The intake worker (still in backend/workers/) loads from new paths."""
    mod = importlib.import_module("backend.workflow.infrastructure.workers.intake_worker")
    assert hasattr(mod, "IntakeWorker")


def test_agent_worker_imports_resolve() -> None:
    mod = importlib.import_module("backend.workflow.infrastructure.workers.agent_worker")
    assert hasattr(mod, "AgentWorker")


def test_schedule_runner_imports_resolve() -> None:
    """Schedule worker is reachable post-Schedule-lift at the new context.

    H3a originally left ``backend.intake.schedule`` in place pending the
    Schedule context lift; that lift moved the runner to
    :mod:`backend.schedule.infrastructure.workers.schedule_worker`. The
    intent of this regression test is "the schedule worker symbol is
    still importable", so the path follows the lift.
    """
    mod = importlib.import_module("backend.schedule.infrastructure.workers.schedule_worker")
    assert hasattr(mod, "ScheduleWorker")


def test_messages_endpoint_imports_resolve() -> None:
    mod = importlib.import_module("backend.api.v1.messages")
    assert hasattr(mod, "router")


def test_webhooks_endpoint_imports_resolve() -> None:
    mod = importlib.import_module("backend.api.webhooks")
    assert hasattr(mod, "router")


def test_workspace_compliance_endpoint_imports_resolve() -> None:
    mod = importlib.import_module("backend.api.v1.workspace_compliance")
    assert hasattr(mod, "router")


def test_connectors_resolver_imports_resolve() -> None:
    """``backend.connectors.resolver`` imported ``TriggerEvent``; must follow the move."""
    mod = importlib.import_module("backend.connectors.resolver")
    assert hasattr(mod, "ConnectorInboundResolver")


def test_workflow_agent_runner_imports_resolve() -> None:
    mod = importlib.import_module("backend.workflow.application.agent_runner")
    assert mod is not None


def test_frame_stage_imports_resolve() -> None:
    mod = importlib.import_module("backend.workflow.application.stages.frame")
    assert mod is not None


# ---------------------------------------------------------------------------
# Delta 6: REST endpoints stay 4-step adapter (D35 strict)
# ---------------------------------------------------------------------------


def test_messages_endpoint_does_not_import_old_intake_paths() -> None:
    """``api/v1/messages.py`` references the new Workflow-side trigger service."""
    import inspect

    import backend.api.v1.messages as mod

    src = inspect.getsource(mod)
    assert "from backend.intake.direct" not in src
    assert "backend.workflow.application.intake.direct" in src


def test_webhooks_endpoint_does_not_import_old_intake_paths() -> None:
    import inspect

    import backend.api.webhooks as mod

    src = inspect.getsource(mod)
    assert "from backend.intake.webhook" not in src
    assert "backend.workflow.application.intake.webhook" in src


def test_workspace_compliance_endpoint_uses_new_intake_path() -> None:
    import inspect

    import backend.api.v1.workspace_compliance as mod

    src = inspect.getsource(mod)
    assert "from backend.intake.db" not in src
    assert "backend.workflow.infrastructure.intake.db" in src
