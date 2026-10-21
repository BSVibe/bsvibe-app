"""Where a workspace's vault and skills live — the one place that decides (#929, B — stage 2).

``knowledge_store_backend`` picks the store:

* ``filesystem`` (default) — the directories they always were:
  ``<knowledge_vault_root>/<region>/<workspace>/`` and ``<skills_root>/<workspace>/``.
* ``postgres`` — ``vault_documents`` rows, which every machine shares. The vault keeps the same
  root path as a *name*, so note identities do not change with the store.

Every caller goes through here, so flipping the setting moves the API, the workers and the MCP
tools at once. docs/design/stateless-workers.md §5.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.config import get_settings
from backend.knowledge.graph.storage import FileSystemStorage, StorageBackend
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.vault_paths import workspace_vault_root

if TYPE_CHECKING:
    from backend.config import Settings


@lru_cache(maxsize=1)
def _session_factory() -> Any:
    """This process's sessions for the row store — built on first use, one engine per process."""
    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415

    from backend.data.engine import create_app_engine  # noqa: PLC0415

    return async_sessionmaker(create_app_engine(), expire_on_commit=False)


def _backend(settings: Settings) -> str:
    backend = settings.knowledge_store_backend
    if backend not in ("filesystem", "postgres"):
        raise ValueError(
            f"knowledge_store_backend must be 'filesystem' or 'postgres', got {backend!r}"
        )
    return backend


def _rows(workspace_id: uuid.UUID, tree: str) -> StorageBackend:
    from backend.knowledge.infrastructure.document_store import (  # noqa: PLC0415
        PgDocumentStorage,
    )

    return PgDocumentStorage(_session_factory(), workspace_id=workspace_id, tree=tree)  # type: ignore[arg-type]


def vault_storage_at(
    root: Path, workspace_id: uuid.UUID | str, *, settings: Settings | None = None
) -> StorageBackend:
    """The store for the vault named ``root`` (files there, or the workspace's rows)."""
    s = settings or get_settings()
    if _backend(s) == "postgres":
        return _rows(uuid.UUID(str(workspace_id)), "vault")
    root.mkdir(parents=True, exist_ok=True)
    return FileSystemStorage(root)


def vault_at(
    root: Path, workspace_id: uuid.UUID | str, *, settings: Settings | None = None
) -> Vault:
    """The vault named ``root`` — for callers that already hold the workspace's root path."""
    return Vault(root, storage=vault_storage_at(root, workspace_id, settings=settings))


async def vault_present(
    root: Path, workspace_id: uuid.UUID | str, *, settings: Settings | None = None
) -> bool:
    """Whether the vault named ``root`` has been started: its directory, or any of its rows."""
    s = settings or get_settings()
    if _backend(s) == "postgres":
        return bool(await _rows(uuid.UUID(str(workspace_id)), "vault").list_files("", "*"))
    return root.exists()


def workspace_vault_storage(
    workspace_id: uuid.UUID, *, settings: Settings | None = None
) -> StorageBackend:
    """The store behind ``workspace_id``'s vault."""
    s = settings or get_settings()
    return vault_storage_at(
        workspace_vault_root(workspace_id, settings=s), workspace_id, settings=s
    )


def workspace_vault(workspace_id: uuid.UUID, *, settings: Settings | None = None) -> Vault:
    """``workspace_id``'s vault, on whichever store is configured."""
    s = settings or get_settings()
    return vault_at(workspace_vault_root(workspace_id, settings=s), workspace_id, settings=s)


def workspace_skills_storage(
    workspace_id: uuid.UUID, *, settings: Settings | None = None
) -> StorageBackend:
    """The store behind ``workspace_id``'s skills (``<slug>.md`` at the top)."""
    s = settings or get_settings()
    if _backend(s) == "postgres":
        return _rows(workspace_id, "skills")
    root = Path(s.skills_root) / str(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    return FileSystemStorage(root)


__all__ = [
    "vault_at",
    "vault_present",
    "vault_storage_at",
    "workspace_skills_storage",
    "workspace_vault",
    "workspace_vault_storage",
]
