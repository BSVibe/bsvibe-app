"""Lift I-Repo-Identity — Protocol smoke tests.

Assert the Identity Repository Protocols exist with the agreed method
shape and that they are :class:`Protocol` types (so any structurally-
conforming class can satisfy them).
"""

from __future__ import annotations

import inspect

import pytest


def test_workspace_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.identity.domain.repositories import WorkspaceRepository

    assert issubclass(WorkspaceRepository, Protocol)  # type: ignore[arg-type]
    for name in ("get", "get_live", "list_for_user", "list_active_regions", "add"):
        method = getattr(WorkspaceRepository, name, None)
        assert method is not None, f"WorkspaceRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_user_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.identity.domain.repositories import UserRepository

    assert issubclass(UserRepository, Protocol)  # type: ignore[arg-type]
    for name in ("get", "get_by_supabase_id", "add", "lock_for_update"):
        method = getattr(UserRepository, name, None)
        assert method is not None, f"UserRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_membership_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.identity.domain.repositories import MembershipRepository

    assert issubclass(MembershipRepository, Protocol)  # type: ignore[arg-type]
    for name in (
        "first_active_for_user",
        "active_for_user_in_workspace",
        "add",
    ):
        method = getattr(MembershipRepository, name, None)
        assert method is not None, f"MembershipRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_concrete_implementations_satisfy_protocols() -> None:
    """The concrete classes must be runtime-checkable as their Protocol."""
    from backend.identity.domain.repositories import (
        MembershipRepository,
        UserRepository,
        WorkspaceRepository,
    )
    from backend.identity.infrastructure.repositories import (
        SqlAlchemyMembershipRepository,
        SqlAlchemyUserRepository,
        SqlAlchemyWorkspaceRepository,
    )

    class _StubSession:
        pass

    workspace_repo = SqlAlchemyWorkspaceRepository(session=_StubSession())  # type: ignore[arg-type]
    user_repo = SqlAlchemyUserRepository(session=_StubSession())  # type: ignore[arg-type]
    membership_repo = SqlAlchemyMembershipRepository(session=_StubSession())  # type: ignore[arg-type]
    assert isinstance(workspace_repo, WorkspaceRepository)
    assert isinstance(user_repo, UserRepository)
    assert isinstance(membership_repo, MembershipRepository)


def test_application_layer_decoupled_from_sqlalchemy_for_chosen_repos() -> None:
    """The application-layer files we refactored must not import the raw
    SQLAlchemy row types for the Repository-covered queries anymore.

    Specifically:

    * ``backend/api/v1/workspace.py`` no longer issues
      ``select(WorkspaceRow)`` (the WorkspaceRepository covers every such
      query in that file).
    * ``backend/api/v1/workspaces.py`` no longer issues
      ``select(WorkspaceRow)`` or ``session.get(WorkspaceRow, ...)``.
    * ``backend/api/v1/workspace_compliance.py`` no longer issues
      ``select(MembershipRow)`` or ``select(WorkspaceRow)``.
    * ``backend/identity/service.py`` no longer issues
      ``select(UserRow)`` or ``select(MembershipRow)``.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[4]

    workspace_route = (repo_root / "backend/api/v1/workspace.py").read_text()
    assert "select(WorkspaceRow)" not in workspace_route, (
        "workspace.py should query workspaces via WorkspaceRepository now"
    )

    workspaces_route = (repo_root / "backend/api/v1/workspaces.py").read_text()
    assert "select(WorkspaceRow)" not in workspaces_route, (
        "workspaces.py should query workspaces via WorkspaceRepository now"
    )
    assert "session.get(WorkspaceRow" not in workspaces_route, (
        "workspaces.py should fetch workspaces via WorkspaceRepository.get_live now"
    )
    assert "select(MembershipRow)" not in workspaces_route, (
        "workspaces.py should query memberships via MembershipRepository now"
    )

    compliance = (repo_root / "backend/api/v1/workspace_compliance.py").read_text()
    assert "select(MembershipRow)" not in compliance, (
        "workspace_compliance.py should query memberships via MembershipRepository now"
    )
    assert "select(WorkspaceRow)" not in compliance, (
        "workspace_compliance.py should query workspaces via WorkspaceRepository now"
    )

    service = (repo_root / "backend/identity/service.py").read_text()
    assert "select(UserRow)" not in service, (
        "identity/service.py should query users via UserRepository now"
    )
    assert "select(MembershipRow)" not in service, (
        "identity/service.py should query memberships via MembershipRepository now"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
