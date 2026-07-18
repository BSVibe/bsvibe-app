"""Lift I-Repo-Workflow — Protocol smoke tests.

Assert the Workflow Repository Protocols exist with the agreed method shape
and that they are :class:`Protocol` types (so any structurally-conforming
class can satisfy them).
"""

from __future__ import annotations

import inspect

import pytest


def test_run_repository_protocol_surface() -> None:
    from typing import Protocol, get_type_hints

    from backend.workflow.domain.repositories import RunRepository

    assert issubclass(RunRepository, Protocol)  # type: ignore[arg-type]
    for name in ("get", "list_by_workspace", "find_by_request_id", "add"):
        method = getattr(RunRepository, name, None)
        assert method is not None, f"RunRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"
    # Resolve the forward-ref to ExecutionRun
    get_type_hints(RunRepository.get)


def test_decision_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.workflow.domain.repositories import DecisionRepository

    assert issubclass(DecisionRepository, Protocol)  # type: ignore[arg-type]
    for name in (
        "get",
        "list_pending_by_workspace",
        "list_resolved_by_workspace",
        "list_by_run",
        "add",
    ):
        method = getattr(DecisionRepository, name, None)
        assert method is not None, f"DecisionRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_deliverable_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.workflow.domain.repositories import DeliverableRepository

    assert issubclass(DeliverableRepository, Protocol)  # type: ignore[arg-type]
    for name in (
        "get",
        "list_by_workspace",
        "list_by_run",
        "list_by_run_id",
        "find_first_by_run",
        "add",
    ):
        method = getattr(DeliverableRepository, name, None)
        assert method is not None, f"DeliverableRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_safe_mode_queue_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.workflow.domain.repositories import SafeModeQueueRepository

    assert issubclass(SafeModeQueueRepository, Protocol)  # type: ignore[arg-type]
    for name in (
        "get",
        "list_pending_by_workspace",
        "list_pending_for_run",
        "list_resolved_by_workspace",
        "list_due_expired",
        "mark_expired_bulk",
        "add",
    ):
        method = getattr(SafeModeQueueRepository, name, None)
        assert method is not None, f"SafeModeQueueRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_request_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.workflow.domain.repositories import RequestRepository

    assert issubclass(RequestRepository, Protocol)  # type: ignore[arg-type]
    for name in (
        "get",
        "list_by_workspace",
        "list_open_for_claim",
        "enqueue",
    ):
        method = getattr(RequestRepository, name, None)
        assert method is not None, f"RequestRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_idempotency_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.workflow.domain.repositories import IdempotencyRepository

    assert issubclass(IdempotencyRepository, Protocol)  # type: ignore[arg-type]
    for name in (
        "is_duplicate",
        "record",
        "list_undrained",
    ):
        method = getattr(IdempotencyRepository, name, None)
        assert method is not None, f"IdempotencyRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_concrete_implementations_satisfy_protocols() -> None:
    """The SQLAlchemy concrete classes must be runtime-checkable as the Protocol."""
    from backend.workflow.domain.repositories import (
        DecisionRepository,
        DeliverableRepository,
        IdempotencyRepository,
        RequestRepository,
        RunRepository,
        SafeModeQueueRepository,
    )
    from backend.workflow.infrastructure.repositories import (
        SqlAlchemyDecisionRepository,
        SqlAlchemyDeliverableRepository,
        SqlAlchemyIdempotencyRepository,
        SqlAlchemyRequestRepository,
        SqlAlchemyRunRepository,
        SqlAlchemySafeModeQueueRepository,
    )

    # The Protocols are @runtime_checkable, so isinstance must accept the
    # concrete impls. (Use a sentinel object that has the methods — the real
    # check is the structural-isinstance below.)
    class _StubSession:
        pass

    run_repo = SqlAlchemyRunRepository(session=_StubSession())  # type: ignore[arg-type]
    dec_repo = SqlAlchemyDecisionRepository(session=_StubSession())  # type: ignore[arg-type]
    del_repo = SqlAlchemyDeliverableRepository(session=_StubSession())  # type: ignore[arg-type]
    smq_repo = SqlAlchemySafeModeQueueRepository(session=_StubSession())  # type: ignore[arg-type]
    req_repo = SqlAlchemyRequestRepository(session=_StubSession())  # type: ignore[arg-type]
    idem_repo = SqlAlchemyIdempotencyRepository(session=_StubSession())  # type: ignore[arg-type]
    assert isinstance(run_repo, RunRepository)
    assert isinstance(dec_repo, DecisionRepository)
    assert isinstance(del_repo, DeliverableRepository)
    assert isinstance(smq_repo, SafeModeQueueRepository)
    assert isinstance(req_repo, RequestRepository)
    assert isinstance(idem_repo, IdempotencyRepository)


def test_application_layer_decoupled_from_sqlalchemy_for_chosen_repos() -> None:
    """The application-layer files we refactored must not import sqlalchemy
    directly anymore for the Repository-covered queries.

    Specifically: ``backend/api/v1/checkpoints.py`` no longer issues a
    ``select(Decision)`` (the DecisionRepository covers every such query),
    and ``backend/workflow/application/agent_runner.py`` no longer issues a
    ``select(ExecutionRun)`` (the RunRepository covers the request-id lookup).
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[4]

    checkpoints = (repo_root / "backend/api/v1/checkpoints.py").read_text()
    assert "select(Decision)" not in checkpoints, (
        "checkpoints.py should query Decisions via DecisionRepository now"
    )

    agent_runner = (repo_root / "backend/workflow/application/agent_runner.py").read_text()
    assert "select(ExecutionRun)" not in agent_runner, (
        "agent_runner.py should query ExecutionRun via RunRepository now"
    )

    # Lift I-Repo-Workflow-2 — Deliverable + SafeModeQueue.
    assert "select(Deliverable)" not in agent_runner, (
        "agent_runner.py should query Deliverable via DeliverableRepository now"
    )

    safe_mode_queue = (repo_root / "backend/workflow/application/safe_mode_queue.py").read_text()
    assert "select(SafeModeQueueItemRow)" not in safe_mode_queue, (
        "safe_mode_queue.py should query items via SafeModeQueueRepository now"
    )
    # _session.get(SafeModeQueueItemRow ...) — same seam, routed through repo
    assert "_session.get(SafeModeQueueItemRow" not in safe_mode_queue, (
        "safe_mode_queue.py should not session.get(SafeModeQueueItemRow ...) directly"
    )

    # Lift §17.9 — deliverables REST is now a package; assertion still holds
    # across all sub-files (none of them should select(Deliverable) directly —
    # the Repository is the only seam).
    deliverables_pkg = repo_root / "backend/api/v1/deliverables"
    deliverables_rest = "\n".join(p.read_text() for p in sorted(deliverables_pkg.glob("*.py")))
    assert "select(Deliverable)" not in deliverables_rest, (
        "backend/api/v1/deliverables/* should use DeliverableRepository, not select(Deliverable)"
    )

    # Lift I-Repo-Workflow-3 — Request + Idempotency.
    webhook = (repo_root / "backend/workflow/application/intake/webhook.py").read_text()
    assert "from backend.workflow.infrastructure.idempotency" not in webhook, (
        "webhook.py should call IdempotencyRepository, not the legacy module helpers"
    )
    assert "TriggerEventRow(" not in webhook, (
        "webhook.py should not instantiate TriggerEventRow directly — route via the Repository"
    )

    direct = (repo_root / "backend/workflow/application/intake/direct.py").read_text()
    assert "from backend.workflow.infrastructure.idempotency" not in direct, (
        "direct.py should call IdempotencyRepository, not the legacy module helpers"
    )
    assert "TriggerEventRow(" not in direct, (
        "direct.py should not instantiate TriggerEventRow directly — route via the Repository"
    )

    workspace_compliance = (repo_root / "backend/api/v1/workspace_compliance.py").read_text()
    assert "select(RequestRow)" not in workspace_compliance, (
        "workspace_compliance.py should list requests via RequestRepository now"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
