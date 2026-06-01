"""VaultNoteRepository — concrete :class:`NoteRepository` over a StorageBackend.

v8 D44/D45. Knowledge's SoT is the filesystem-backed Vault; this concrete
delegates each Protocol method to the existing
:class:`~backend.knowledge.graph.storage.StorageBackend` (production:
:class:`~backend.knowledge.graph.storage.FileSystemStorage`). The repository
holds a backend bound to one workspace boundary
(``<vault_root>/<region>/<workspace_id>/``) — workspace scoping is
structural (the bound storage cannot escape its root), matching the
:class:`~backend.knowledge.factory.KnowledgeFactory` convention.
"""

from __future__ import annotations

from backend.knowledge.graph.storage import StorageBackend


class VaultNoteRepository:
    """Vault-backed :class:`~backend.knowledge.domain.repositories.NoteRepository`.

    Constructor-injected with one :class:`StorageBackend`. The backend owns
    its workspace boundary; the repository is a thin Protocol adapter so
    application code can depend on the
    :class:`~backend.knowledge.domain.repositories.NoteRepository` Protocol
    instead of the concrete backend.
    """

    __slots__ = ("_storage",)

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    async def read(self, path: str) -> str:
        return await self._storage.read(path)

    async def exists(self, path: str) -> bool:
        return await self._storage.exists(path)

    async def list_paths(self, subdir: str, *, pattern: str = "*.md") -> list[str]:
        return await self._storage.list_files(subdir, pattern=pattern)

    async def write(self, path: str, content: str) -> None:
        await self._storage.write(path, content)

    async def delete(self, path: str) -> None:
        await self._storage.delete(path)


__all__ = ["VaultNoteRepository"]
