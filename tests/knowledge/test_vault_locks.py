"""#929 (B), stage 2 — a vault write that reads first is serialized across writers.

The garden writer and the canonicalization service guarded their read-modify-write sections
with ``asyncio.Lock``\\ s created PER INSTANCE — and an instance is made per request. So two
requests, or the API and the worker, never excluded each other: an action-log append or a
frontmatter edit could lose the other's update. On rows (``vault_documents``), where an append
is a read and a write, that is an ordinary race.

The lock now belongs to the vault's store, keyed by (workspace, name):

* rows — a Postgres advisory lock, which holds across every connection and machine;
* files — an ``flock`` on the shared volume, which holds across processes on the box.

Both also hold within a process across instances.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault import Vault
from backend.knowledge.infrastructure.document_store import PgDocumentStorage

from .._support import db_engine, pg_url, use_real_pg


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[Any]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(params=["files", "rows"])
def make_storage(request, tmp_path: Path, sf):
    ws = uuid.uuid4()

    def _make(workspace: uuid.UUID | None = None) -> Any:
        w = workspace or ws
        if request.param == "files":
            return FileSystemStorage(tmp_path / "vault" / str(w))
        return PgDocumentStorage(sf, workspace_id=w, tree="vault")

    return _make


async def _overlaps(a: Any, b: Any, name_a: str, name_b: str) -> bool:
    """Whether two critical sections under these locks ran at the same time."""
    inside: list[str] = []
    overlapped = False

    async def _section(storage: Any, name: str, tag: str) -> None:
        nonlocal overlapped
        async with storage.lock(name):
            inside.append(tag)
            if len(inside) > 1:
                overlapped = True
            await asyncio.sleep(0.2)
            inside.remove(tag)

    await asyncio.gather(_section(a, name_a, "a"), _section(b, name_b, "b"))
    return overlapped


async def test_two_instances_exclude_each_other(make_storage) -> None:
    assert not await _overlaps(make_storage(), make_storage(), "garden", "garden")


async def test_different_names_do_not_wait_on_each_other(make_storage) -> None:
    assert await _overlaps(make_storage(), make_storage(), "garden", "log")


async def test_different_workspaces_do_not_wait_on_each_other(make_storage) -> None:
    assert await _overlaps(
        make_storage(uuid.uuid4()), make_storage(uuid.uuid4()), "garden", "garden"
    )


async def test_concurrent_appends_from_two_writers_lose_nothing(make_storage, tmp_path) -> None:
    """The race the per-instance locks let through: an append is a read and a write."""
    from backend.knowledge.graph.writer_core import GardenWriter

    root = tmp_path / "vault-root"
    writers = [GardenWriter(vault=Vault(root, storage=make_storage())) for _ in range(2)]

    await asyncio.gather(
        *(
            w.write_action("skill", f"entry {i}-{n}")
            for n in range(8)
            for i, w in enumerate(writers)
        )
    )

    vault = Vault(root, storage=make_storage())
    (log,) = await vault.read_notes("actions")
    text = await vault.read(log)
    assert sum(f"entry {i}-{n}" in text for i in range(2) for n in range(8)) == 16


async def test_the_canonicalization_mutation_lock_holds_across_instances(make_storage) -> None:
    from backend.knowledge.canonicalization.lock import StorageMutationLock

    inside: list[int] = []
    overlapped = False

    async def _apply(n: int) -> None:
        nonlocal overlapped
        async with StorageMutationLock(make_storage()).guard("actions/merge/x.md"):
            inside.append(n)
            overlapped = overlapped or len(inside) > 1
            await asyncio.sleep(0.1)
            inside.remove(n)

    await asyncio.gather(_apply(1), _apply(2))
    assert not overlapped


# --- across processes ----------------------------------------------------------------------------

_HOLDER = """
import asyncio, sys, time, uuid
from pathlib import Path
async def main():
    kind, where, ws = sys.argv[1], sys.argv[2], sys.argv[3]
    if kind == "files":
        from backend.knowledge.graph.storage import FileSystemStorage
        storage = FileSystemStorage(Path(where))
    else:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from backend.knowledge.infrastructure.document_store import PgDocumentStorage
        engine = create_async_engine(where)
        storage = PgDocumentStorage(async_sessionmaker(engine), workspace_id=uuid.UUID(ws), tree="vault")
    async with storage.lock("garden"):
        print("held", flush=True)
        time.sleep(1.5)
asyncio.run(main())
"""


async def _held_elsewhere(kind: str, where: str, ws: uuid.UUID) -> subprocess.Popen[str]:
    proc = subprocess.Popen(  # noqa: S603, ASYNC220 — this test's own interpreter, started once
        [sys.executable, "-c", _HOLDER, kind, where, str(ws)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = await asyncio.to_thread(proc.stdout.readline)
    assert line.strip() == "held"
    return proc


async def test_a_file_lock_holds_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "vault" / "ws"
    proc = await _held_elsewhere("files", str(root), uuid.uuid4())
    started = time.monotonic()
    async with FileSystemStorage(root).lock("garden"):
        waited = time.monotonic() - started
    proc.wait(timeout=10)
    assert waited > 0.8, "the other process held it"


@pytest.mark.skipif(not use_real_pg(), reason="advisory locks are a Postgres feature")
async def test_a_row_lock_holds_across_connections_and_processes() -> None:
    ws = uuid.uuid4()
    proc = await _held_elsewhere("rows", pg_url(), ws)
    engine = create_async_engine(pg_url())
    try:
        storage = PgDocumentStorage(async_sessionmaker(engine), workspace_id=ws, tree="vault")
        started = time.monotonic()
        async with storage.lock("garden"):
            waited = time.monotonic() - started
    finally:
        await engine.dispose()
    proc.wait(timeout=10)
    assert waited > 0.8, "another machine's connection held it"
