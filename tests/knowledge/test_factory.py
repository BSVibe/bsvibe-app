"""KnowledgeFactory smoke — workspace-scoped vault path + per-instance Vault."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.knowledge import KnowledgeFactory, WorkspaceContext


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "vault"


def test_workspace_path_is_region_then_workspace(vault_root: Path) -> None:
    ws_id = str(uuid.uuid4())
    factory = KnowledgeFactory(region="us-1", workspace_id=ws_id, vault_root=vault_root)
    assert factory.vault_path == vault_root / "us-1" / ws_id


def test_context_exposes_region_and_workspace(vault_root: Path) -> None:
    ws_id = str(uuid.uuid4())
    factory = KnowledgeFactory(region="eu-1", workspace_id=ws_id, vault_root=vault_root)
    ctx = factory.context
    assert isinstance(ctx, WorkspaceContext)
    assert ctx.region == "eu-1"
    assert ctx.workspace_id == ws_id


def test_vault_is_constructed_lazily_and_memoized(vault_root: Path) -> None:
    factory = KnowledgeFactory(region="us-1", workspace_id=str(uuid.uuid4()), vault_root=vault_root)
    v1 = factory.vault()
    v2 = factory.vault()
    assert v1 is v2  # memoized per-factory


def test_two_workspaces_get_isolated_paths(vault_root: Path) -> None:
    a = KnowledgeFactory(region="us-1", workspace_id="aa" * 16, vault_root=vault_root)
    b = KnowledgeFactory(region="us-1", workspace_id="bb" * 16, vault_root=vault_root)
    assert a.vault_path != b.vault_path
    a.vault()
    b.vault()
    # Each gets its own on-disk dir.
    assert a.vault_path.exists()
    assert b.vault_path.exists()


def test_vault_path_is_created_on_first_access(vault_root: Path) -> None:
    factory = KnowledgeFactory(region="us-1", workspace_id=str(uuid.uuid4()), vault_root=vault_root)
    assert not factory.vault_path.exists()
    factory.vault()
    assert factory.vault_path.exists()


def test_restricted_plugin_garden_reexported_from_graph() -> None:
    from backend.knowledge.graph import RestrictedPluginGarden

    assert RestrictedPluginGarden.__name__ == "RestrictedPluginGarden"
