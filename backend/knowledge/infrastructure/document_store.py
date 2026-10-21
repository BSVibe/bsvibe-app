"""The vault and skills as Postgres rows (#929, B — stage 2).

The vault's Markdown is its source of truth, and until now it lived as files on one machine's
disk, written by the API process and the worker process alike. With more than one machine
every reader and writer has to see the same documents, so they move into one table —
``vault_documents`` — without changing what they are: a row's ``content`` is exactly the file,
and its ``path`` is exactly the file's tree-relative path. docs/design/stateless-workers.md §5.

Layer-2 table (the ORM auto-filter scopes it by ``workspace_id``), like ``note_embeddings``; each
operation also publishes the workspace to the RLS GUC through ``workspace_session_scope``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import posixpath
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import DateTime, String, Text, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base
from backend.data.rls import workspace_session_scope
from backend.knowledge.graph.storage import StorageBackend

#: Which tree a document belongs to: the knowledge vault, or the workspace's skills.
DocumentTree = Literal["vault", "skills"]


class VaultDocumentRow(Base):
    """One vault (or skills) file: its Markdown/JSON exactly, at its tree-relative path."""

    __tablename__ = "vault_documents"

    workspace_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tree: Mapped[str] = mapped_column(String(16), primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: sha256 of ``content`` alone — what embedding reconcile compares.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _normalize(rel_path: str) -> str:
    """The canonical spelling of a tree-relative path; refuses one that leaves the tree."""
    if rel_path.startswith("/"):
        raise ValueError(f"Path escapes storage root: {rel_path}")
    norm = posixpath.normpath(rel_path or ".")
    if norm == ".." or norm.startswith("../"):
        raise ValueError(f"Path escapes storage root: {rel_path}")
    return "" if norm == "." else norm


def _like_prefix(subdir: str) -> str:
    """``LIKE`` pattern for everything under ``subdir``, its wildcards taken literally."""
    escaped = subdir.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}/%" if escaped else "%"


class PgDocumentStorage(StorageBackend):
    """:class:`StorageBackend` over ``vault_documents`` for one workspace's tree.

    Each call runs in its own short transaction, so it can be used from anywhere a file path
    could be — inside a request, a worker tick, or a background task.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[Any],
        *,
        workspace_id: uuid.UUID,
        tree: DocumentTree,
    ) -> None:
        self._sf = session_factory
        self._ws = workspace_id
        self._tree = tree

    def _here(self) -> Any:
        """This store's rows: its workspace and its tree."""
        return (VaultDocumentRow.workspace_id == self._ws) & (VaultDocumentRow.tree == self._tree)

    async def read(self, rel_path: str) -> str:
        path = _normalize(rel_path)
        async with self._sf() as session, workspace_session_scope(session, self._ws):
            content = await session.scalar(
                select(VaultDocumentRow.content).where(
                    self._here(),
                    VaultDocumentRow.path == path,
                )
            )
        if content is None:
            raise FileNotFoundError(rel_path)
        return str(content)

    async def read_many(self, rel_paths: list[str]) -> dict[str, str]:
        by_norm = {_normalize(p): p for p in rel_paths}
        if not by_norm:
            return {}
        async with self._sf() as session, workspace_session_scope(session, self._ws):
            rows = (
                await session.execute(
                    select(VaultDocumentRow.path, VaultDocumentRow.content).where(
                        self._here(),
                        VaultDocumentRow.path.in_(list(by_norm)),
                    )
                )
            ).all()
        return {by_norm[path]: content for path, content in rows}

    async def write(self, rel_path: str, content: str) -> None:
        path = _normalize(rel_path)
        if not path:
            raise ValueError(f"not a document path: {rel_path!r}")
        now = datetime.now(tz=UTC)
        values = {
            "workspace_id": self._ws,
            "tree": self._tree,
            "path": path,
            "content": content,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "created_at": now,
            "updated_at": now,
        }
        async with self._sf() as session, workspace_session_scope(session, self._ws):
            insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
            stmt = insert(VaultDocumentRow).values(**values)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["workspace_id", "tree", "path"],
                    set_={
                        "content": stmt.excluded.content,
                        "content_hash": stmt.excluded.content_hash,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
            )
            await session.commit()

    async def create(self, rel_path: str, content: str) -> bool:
        path = _normalize(rel_path)
        if not path:
            raise ValueError(f"not a document path: {rel_path!r}")
        now = datetime.now(tz=UTC)
        async with self._sf() as session, workspace_session_scope(session, self._ws):
            insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
            result = await session.execute(
                insert(VaultDocumentRow)
                .values(
                    workspace_id=self._ws,
                    tree=self._tree,
                    path=path,
                    content=content,
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["workspace_id", "tree", "path"])
            )
            await session.commit()
        return bool(result.rowcount)

    async def delete(self, rel_path: str) -> None:
        path = _normalize(rel_path)
        async with self._sf() as session, workspace_session_scope(session, self._ws):
            await session.execute(
                delete(VaultDocumentRow).where(self._here(), VaultDocumentRow.path == path)
            )
            await session.commit()

    async def delete_prefix(self, subdir: str) -> None:
        prefix = _normalize(subdir)
        async with self._sf() as session, workspace_session_scope(session, self._ws):
            await session.execute(
                delete(VaultDocumentRow).where(
                    self._here(),
                    VaultDocumentRow.path.like(_like_prefix(prefix), escape="\\"),
                )
            )
            await session.commit()

    async def exists(self, rel_path: str) -> bool:
        path = _normalize(rel_path)
        async with self._sf() as session, workspace_session_scope(session, self._ws):
            found = await session.scalar(
                select(VaultDocumentRow.path).where(
                    self._here(),
                    VaultDocumentRow.path == path,
                )
            )
        return found is not None

    async def list_files(self, subdir: str, pattern: str = "*.md") -> list[str]:
        prefix = _normalize(subdir)
        async with self._sf() as session, workspace_session_scope(session, self._ws):
            paths = (
                await session.scalars(
                    select(VaultDocumentRow.path).where(
                        self._here(),
                        VaultDocumentRow.path.like(_like_prefix(prefix), escape="\\"),
                    )
                )
            ).all()
        # ``rglob(pattern)`` semantics: the pattern matches the trailing component(s).
        depth = pattern.count("/") + 1
        return sorted(
            p for p in paths if fnmatch.fnmatchcase("/".join(p.split("/")[-depth:]), pattern)
        )

    async def content_hash(self, rel_path: str) -> str:
        content = await self.read(rel_path)
        payload = content.encode("utf-8") + b"\x00" + rel_path.encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


__all__ = ["DocumentTree", "PgDocumentStorage", "VaultDocumentRow"]
