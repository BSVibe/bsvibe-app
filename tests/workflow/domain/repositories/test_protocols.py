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


def test_concrete_implementations_satisfy_protocols() -> None:
    """The SQLAlchemy concrete classes must be runtime-checkable as the Protocol."""
    from backend.workflow.domain.repositories import DecisionRepository, RunRepository
    from backend.workflow.infrastructure.repositories import (
        SqlAlchemyDecisionRepository,
        SqlAlchemyRunRepository,
    )

    # The Protocols are @runtime_checkable, so isinstance must accept the
    # concrete impls. (Use a sentinel object that has the methods — the real
    # check is the structural-isinstance below.)
    class _StubSession:
        pass

    run_repo = SqlAlchemyRunRepository(session=_StubSession())  # type: ignore[arg-type]
    dec_repo = SqlAlchemyDecisionRepository(session=_StubSession())  # type: ignore[arg-type]
    assert isinstance(run_repo, RunRepository)
    assert isinstance(dec_repo, DecisionRepository)


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


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
