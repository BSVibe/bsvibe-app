"""KnowledgeFactory smoke — workspace-scoped vault path + per-instance Vault."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.config import get_settings
from backend.knowledge import KnowledgeFactory, WorkspaceContext
from backend.knowledge.graph.vault_paths import workspace_vault_root


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "vault"


def test_workspace_path_is_deployment_region_then_workspace(vault_root: Path) -> None:
    ws_id = str(uuid.uuid4())
    factory = KnowledgeFactory(workspace_id=ws_id, vault_root=vault_root)
    assert factory.vault_path == vault_root / get_settings().knowledge_default_region / ws_id


def test_the_factory_agrees_with_vault_paths(vault_root: Path) -> None:
    """The factory is the ONE other place the vault layout is composed.

    It keeps an injectable ``vault_root`` because tests point it at a
    ``tmp_path``; ``vault_paths.workspace_vault_root`` reads the configured
    root instead. Two constructions of the same layout can drift, so pin them
    to each other rather than pretending there is only one.
    """
    settings = get_settings()
    ws_id = uuid.uuid4()
    factory = KnowledgeFactory(
        workspace_id=str(ws_id), vault_root=Path(settings.knowledge_vault_root)
    )
    assert factory.vault_path == workspace_vault_root(ws_id)


def test_context_exposes_the_workspace(vault_root: Path) -> None:
    ws_id = str(uuid.uuid4())
    factory = KnowledgeFactory(workspace_id=ws_id, vault_root=vault_root)
    ctx = factory.context
    assert isinstance(ctx, WorkspaceContext)
    assert ctx.workspace_id == ws_id


def test_vault_is_constructed_lazily_and_memoized(vault_root: Path) -> None:
    factory = KnowledgeFactory(workspace_id=str(uuid.uuid4()), vault_root=vault_root)
    v1 = factory.vault()
    v2 = factory.vault()
    assert v1 is v2  # memoized per-factory


def test_two_workspaces_get_isolated_paths(vault_root: Path) -> None:
    a = KnowledgeFactory(workspace_id="aa" * 16, vault_root=vault_root)
    b = KnowledgeFactory(workspace_id="bb" * 16, vault_root=vault_root)
    assert a.vault_path != b.vault_path
    a.vault()
    b.vault()
    # Each gets its own on-disk dir.
    assert a.vault_path.exists()
    assert b.vault_path.exists()


def test_vault_path_is_created_on_first_access(vault_root: Path) -> None:
    factory = KnowledgeFactory(workspace_id=str(uuid.uuid4()), vault_root=vault_root)
    assert not factory.vault_path.exists()
    factory.vault()
    assert factory.vault_path.exists()


def test_restricted_plugin_garden_reexported_from_graph() -> None:
    from backend.knowledge.graph import RestrictedPluginGarden

    assert RestrictedPluginGarden.__name__ == "RestrictedPluginGarden"


def test_factory_writer_is_workspace_scoped(vault_root: Path) -> None:
    """GardenWriter from factory writes inside the workspace's vault path."""
    factory = KnowledgeFactory(workspace_id=str(uuid.uuid4()), vault_root=vault_root)
    writer = factory.writer()
    assert writer is factory.writer()  # memoized
    # The bound vault must point at the workspace-scoped path
    assert writer._vault.root == factory.vault_path  # noqa: SLF001 — invariant assert


def test_factory_restricted_garden_uses_writer(vault_root: Path) -> None:
    factory = KnowledgeFactory(workspace_id=str(uuid.uuid4()), vault_root=vault_root)
    rpg = factory.restricted_garden()
    # Blocked methods raise PermissionError
    import pytest as _pytest

    with _pytest.raises(PermissionError):
        rpg.write_garden  # noqa: B018 — attribute access triggers __getattr__


def test_factory_writer_is_isolated_per_workspace(vault_root: Path) -> None:
    a = KnowledgeFactory(workspace_id="aa" * 16, vault_root=vault_root)
    b = KnowledgeFactory(workspace_id="bb" * 16, vault_root=vault_root)
    wa, wb = a.writer(), b.writer()
    # Each writer's vault root is distinct
    assert wa._vault.root != wb._vault.root  # noqa: SLF001
