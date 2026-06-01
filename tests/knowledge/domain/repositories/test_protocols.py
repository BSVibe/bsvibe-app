"""Lift I-Repo-Knowledge — Protocol smoke tests.

Assert the Knowledge Repository Protocols exist with the agreed method
shape and that they are :class:`Protocol` types (so any structurally-
conforming class can satisfy them).
"""

from __future__ import annotations

import inspect

import pytest


def test_note_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.knowledge.domain.repositories import NoteRepository

    assert issubclass(NoteRepository, Protocol)  # type: ignore[arg-type]
    for name in ("read", "exists", "list_paths", "write", "delete"):
        method = getattr(NoteRepository, name, None)
        assert method is not None, f"NoteRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_proposal_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.knowledge.domain.repositories import ProposalRepository

    assert issubclass(ProposalRepository, Protocol)  # type: ignore[arg-type]
    for name in (
        "get",
        "list_by_workspace",
        "list_pending_by_workspace",
        "list_by_status",
        "add",
    ):
        method = getattr(ProposalRepository, name, None)
        assert method is not None, f"ProposalRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_canonical_anchor_repository_protocol_surface() -> None:
    from typing import Protocol

    from backend.knowledge.domain.repositories import CanonicalAnchorRepository

    assert issubclass(CanonicalAnchorRepository, Protocol)  # type: ignore[arg-type]
    for name in ("get", "find_by_name", "list_by_workspace", "add"):
        method = getattr(CanonicalAnchorRepository, name, None)
        assert method is not None, f"CanonicalAnchorRepository missing {name}"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_concrete_implementations_satisfy_protocols() -> None:
    """The concrete classes must be runtime-checkable as the Protocol."""
    from backend.knowledge.domain.repositories import (
        CanonicalAnchorRepository,
        NoteRepository,
        ProposalRepository,
    )
    from backend.knowledge.infrastructure.repositories import (
        SqlAlchemyCanonicalAnchorRepository,
        SqlAlchemyProposalRepository,
        VaultNoteRepository,
    )

    class _StubSession:
        pass

    class _StubStorage:
        async def read(self, rel_path: str) -> str:  # pragma: no cover - stub
            return ""

        async def write(self, rel_path: str, content: str) -> None:  # pragma: no cover - stub
            return None

        async def delete(self, rel_path: str) -> None:  # pragma: no cover - stub
            return None

        async def exists(self, rel_path: str) -> bool:  # pragma: no cover - stub
            return False

        async def list_files(  # pragma: no cover - stub
            self, subdir: str, pattern: str = "*.md"
        ) -> list[str]:
            return []

        async def content_hash(self, rel_path: str) -> str:  # pragma: no cover - stub
            return ""

    note_repo = VaultNoteRepository(_StubStorage())  # type: ignore[arg-type]
    proposal_repo = SqlAlchemyProposalRepository(session=_StubSession())  # type: ignore[arg-type]
    anchor_repo = SqlAlchemyCanonicalAnchorRepository(session=_StubSession())  # type: ignore[arg-type]
    assert isinstance(note_repo, NoteRepository)
    assert isinstance(proposal_repo, ProposalRepository)
    assert isinstance(anchor_repo, CanonicalAnchorRepository)


def test_application_layer_decoupled_from_sqlalchemy_for_chosen_repos() -> None:
    """The application-layer files we refactored must not import the raw
    SQLAlchemy row types for the Repository-covered queries anymore.

    Specifically: ``backend/api/v1/workspace_compliance.py`` no longer issues
    a ``select(CanonicalAnchor)`` (the CanonicalAnchorRepository covers
    every such query in that file).
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[4]

    workspace_compliance = (repo_root / "backend/api/v1/workspace_compliance.py").read_text()
    assert "select(CanonicalAnchor)" not in workspace_compliance, (
        "workspace_compliance.py should query anchors via CanonicalAnchorRepository now"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
