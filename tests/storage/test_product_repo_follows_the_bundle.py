"""#929 (B) — a product repo already on this machine follows what another machine published.

``ensure_product_workspace`` short-circuited on "a repo is present": the local copy always
won, because it can carry commits newer than the last published bundle and clobbering it
would destroy them. True — and with two machines it also meant machine B kept working from
the ``main`` it materialised days ago while machine A shipped on top of the bundle.

Now a present repo still wins, but it is brought FORWARD:

* the bundle's version (S3 ETag / local size+mtime) is checked first — unchanged → no
  download at all;
* the bundle's ``main`` is a descendant of the local ``main`` → fast-forward;
* local ``main`` has commits the bundle lacks (diverged or ahead) → the local copy is kept
  untouched (publish's merge-not-overwrite reconciles it later), and that is logged;
* any failure (store down, git lock) → the local copy is used as before; never an error.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from backend.config import get_settings
from backend.storage.product_bundle_store import LocalFilesystemBundleStore
from backend.storage.product_workspace import (
    ensure_product_workspace,
    init_product_workspace,
    product_workspace_path,
    publish_product_bundle,
)

pytestmark = pytest.mark.asyncio


async def _git(*args: str, cwd: Path) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return out.decode().strip()


class _Machine:
    """One box: its own product root, the shared bundle store."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self._mp = monkeypatch

    def __enter__(self) -> _Machine:
        self._mp.setattr(get_settings(), "product_workspace_root", str(self.root), raising=False)
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _CountingStore(LocalFilesystemBundleStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.gets = 0

    async def get(self, product_id: uuid.UUID, dest_path: Path) -> bool:
        self.gets += 1
        return await super().get(product_id, dest_path)


async def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    await _git("add", name, cwd=repo)
    await _git("commit", "-qm", name, cwd=repo)
    return await _git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store = _CountingStore(tmp_path / "bundles")
    return {
        "store": store,
        "a": _Machine(tmp_path / "a", monkeypatch),
        "b": _Machine(tmp_path / "b", monkeypatch),
    }


async def _shared_product(world: dict[str, Any]) -> uuid.UUID:
    """A product shipped on machine A and materialised on machine B."""
    pid = uuid.uuid4()
    with world["a"]:
        await init_product_workspace(pid)
        await publish_product_bundle(pid, store=world["store"])
    with world["b"]:
        assert await ensure_product_workspace(pid, store=world["store"])
    return pid


async def test_machine_b_follows_what_machine_a_shipped(world: dict[str, Any]) -> None:
    pid = await _shared_product(world)
    with world["a"]:
        shipped = await _commit(product_workspace_path(pid), "feature.py")
        await publish_product_bundle(pid, store=world["store"])

    with world["b"]:
        assert await ensure_product_workspace(pid, store=world["store"])
        repo = product_workspace_path(pid)
        assert await _git("rev-parse", "main", cwd=repo) == shipped
        assert (repo / "feature.py").read_text() == "feature.py", "the checkout moved with main"


async def test_an_unchanged_bundle_is_not_downloaded_again(world: dict[str, Any]) -> None:
    pid = await _shared_product(world)
    with world["b"]:
        await ensure_product_workspace(pid, store=world["store"])
        before = world["store"].gets
        await ensure_product_workspace(pid, store=world["store"])
        await ensure_product_workspace(pid, store=world["store"])

    assert world["store"].gets == before, "a cheap version check must answer 'nothing new'"


async def test_local_work_the_bundle_lacks_is_never_overwritten(world: dict[str, Any]) -> None:
    pid = await _shared_product(world)
    with world["b"]:
        local = await _commit(product_workspace_path(pid), "local-only.py")
    with world["a"]:
        await _commit(product_workspace_path(pid), "remote.py")
        await publish_product_bundle(pid, store=world["store"])

    with world["b"], capture_logs() as logs:
        assert await ensure_product_workspace(pid, store=world["store"])
        assert await _git("rev-parse", "main", cwd=product_workspace_path(pid)) == local

    assert any(e["event"] == "product_repo_diverged_from_bundle" for e in logs)


async def test_a_store_failure_keeps_the_local_copy(world: dict[str, Any]) -> None:
    pid = await _shared_product(world)

    class _Down(LocalFilesystemBundleStore):
        async def version(self, product_id: uuid.UUID) -> str | None:
            raise ConnectionError("store down")

    with world["b"]:
        repo = product_workspace_path(pid)
        head = await _git("rev-parse", "main", cwd=repo)
        assert await ensure_product_workspace(pid, store=_Down(Path("/nonexistent")))
        assert await _git("rev-parse", "main", cwd=repo) == head


async def test_the_local_store_version_changes_when_the_bundle_does(tmp_path: Path) -> None:
    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    pid = uuid.uuid4()
    assert await store.version(pid) is None
    src = tmp_path / "x.bundle"
    src.write_bytes(b"one")
    await store.put(pid, src)
    first = await store.version(pid)
    src.write_bytes(b"two-two")
    await store.put(pid, src)

    assert first is not None and await store.version(pid) != first
