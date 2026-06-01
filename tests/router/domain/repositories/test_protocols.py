"""Lift I-Repo-Router — Protocol smoke tests.

Assert the Router Repository Protocols exist with the agreed method shape,
that they are :class:`Protocol` types (so any structurally-conforming class
can satisfy them), and that the concrete SQL impls satisfy the runtime
isinstance check. Mirror of
``tests/workflow/domain/repositories/test_protocols.py``.
"""

from __future__ import annotations

import inspect

import pytest


def test_model_account_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.router.domain.repositories import ModelAccountRepository

    assert issubclass(ModelAccountRepository, Protocol)  # type: ignore[arg-type]
    for name in (
        "create",
        "get",
        "list_for_account",
        "list_active_for_workspace",
        "list_executor_accounts_for_worker",
        "delete",
        "update",
    ):
        method = getattr(ModelAccountRepository, name, None)
        assert method is not None, f"ModelAccountRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_run_routing_rule_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.router.domain.repositories import RunRoutingRuleRepository

    assert issubclass(RunRoutingRuleRepository, Protocol)  # type: ignore[arg-type]
    for name in ("list_by_workspace", "get", "has_any", "add", "delete"):
        method = getattr(RunRoutingRuleRepository, name, None)
        assert method is not None, f"RunRoutingRuleRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_concrete_implementations_satisfy_protocols() -> None:
    """The SQLAlchemy concrete classes must be runtime-checkable as the Protocol."""
    from backend.router.domain.repositories import (
        ModelAccountRepository,
        RunRoutingRuleRepository,
    )
    from backend.router.infrastructure.repositories import (
        SqlAlchemyModelAccountRepository,
        SqlAlchemyRunRoutingRuleRepository,
    )

    class _StubSession:
        pass

    ma_repo = SqlAlchemyModelAccountRepository(session=_StubSession())  # type: ignore[arg-type]
    rule_repo = SqlAlchemyRunRoutingRuleRepository(session=_StubSession())  # type: ignore[arg-type]
    assert isinstance(ma_repo, ModelAccountRepository)
    assert isinstance(rule_repo, RunRoutingRuleRepository)


def test_application_layer_decoupled_from_sqlalchemy_for_router_repos() -> None:
    """The application-layer files we refactored must not issue raw
    ``select(ModelAccount)`` / ``select(RunRoutingRuleRow)`` queries anymore.

    Specifically:

    * ``backend/api/v1/run_routing.py`` no longer issues ``select(RunRoutingRuleRow)``
      or ``session.get(RunRoutingRuleRow, ...)`` (the RunRoutingRuleRepository
      covers list / get / delete).
    * ``backend/workflow/application/agent_runner.py`` no longer issues
      ``select(RunRoutingRuleRow)`` (the RunRoutingRuleRepository.has_any
      gate covers the Workflow→Router cross-reference).
    * ``backend/workflow/infrastructure/workers/run.py`` no longer issues
      ``select(ModelAccount)`` (the ModelAccountRepository.list_active_for_workspace
      covers the run resolver's roster fetch).
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[4]

    run_routing_rest = (repo_root / "backend/api/v1/run_routing.py").read_text()
    assert "select(RunRoutingRuleRow)" not in run_routing_rest, (
        "run_routing.py REST list should use RunRoutingRuleRepository now"
    )
    assert "session.get(RunRoutingRuleRow" not in run_routing_rest, (
        "run_routing.py REST delete should use RunRoutingRuleRepository now"
    )

    agent_runner = (repo_root / "backend/workflow/application/agent_runner.py").read_text()
    assert "select(RunRoutingRuleRow" not in agent_runner, (
        "agent_runner.py should use RunRoutingRuleRepository.has_any now"
    )

    run_worker = (repo_root / "backend/workflow/infrastructure/workers/run.py").read_text()
    assert "select(ModelAccount)" not in run_worker, (
        "run.py should use ModelAccountRepository.list_active_for_workspace now"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
