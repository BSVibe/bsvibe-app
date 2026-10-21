"""#929 — a product cloned shallow can still reach the bundle store.

Product bootstrap cloned with ``--depth=1``. A shallow repo cannot produce a restorable bundle,
so publish tries ``git fetch --unshallow`` — anonymously, and the clone token was (rightly) never
written to ``.git/config``. For a private repository that repair always failed, the product was
never published (``product_bundle_publish_skipped_shallow``, seen in prod 2026-10-21 for the
BStockReport product), and a second machine could never have restored it.

Publish now takes a ``credential`` callback: asked only when the repo is still shallow after the
plain repair, used for one authenticated fetch, and never written to disk.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from backend.config import get_settings
from backend.storage import product_workspace
from backend.storage.product_bundle_store import LocalFilesystemBundleStore
from backend.storage.product_workspace import product_workspace_path, publish_product_bundle

_PRIVATE = "https://invalid.invalid/acme/private.git"


@pytest.fixture(autouse=True)
def _roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "product_workspace_root", str(tmp_path / "products"), raising=False)
    monkeypatch.setattr(s, "run_workspace_root", str(tmp_path / "runs"), raising=False)


async def _git(*args: str, cwd: Path) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return out.decode().strip()


@pytest.fixture
async def shallow_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """A shallow product clone whose origin is a private HTTPS repository."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    await _git("init", "--initial-branch=main", cwd=upstream)
    await _git("config", "user.email", "u@e.dev", cwd=upstream)
    await _git("config", "user.name", "U", cwd=upstream)
    for i in range(3):
        (upstream / f"f{i}.txt").write_text(f"{i}\n")
        await _git("add", "-A", cwd=upstream)
        await _git("commit", "-m", f"c{i}", cwd=upstream)
    product_id = uuid.uuid4()
    repo = product_workspace_path(product_id)
    repo.parent.mkdir(parents=True, exist_ok=True)
    await _git("clone", "--depth", "1", f"file://{upstream}", str(repo), cwd=tmp_path)
    await _git("remote", "set-url", "origin", _PRIVATE, cwd=repo)

    # The authenticated URL reaches the upstream only with the right token — standing in for
    # github answering a private clone.
    def _authed(url: str, token: str) -> str:
        return f"file://{upstream}" if (url, token) == (_PRIVATE, "t0k") else url

    monkeypatch.setattr(product_workspace, "_authed_fetch_url", _authed)
    return product_id


async def test_without_a_credential_a_private_shallow_repo_still_is_not_published(
    shallow_private, tmp_path: Path
) -> None:
    store = LocalFilesystemBundleStore(tmp_path / "bundles")

    outcome = await publish_product_bundle(shallow_private, store=store)

    assert outcome.published is False and not await store.exists(shallow_private)


async def test_the_credential_repairs_it_and_it_publishes(shallow_private, tmp_path: Path) -> None:
    store = LocalFilesystemBundleStore(tmp_path / "bundles")
    asked: list[bool] = []

    async def _credential() -> str | None:
        asked.append(True)
        return "t0k"

    outcome = await publish_product_bundle(shallow_private, store=store, credential=_credential)

    repo = product_workspace_path(shallow_private)
    assert outcome.published is True and await store.exists(shallow_private)
    assert await _git("rev-parse", "--is-shallow-repository", cwd=repo) == "false"
    assert asked == [True]
    assert "t0k" not in (repo / ".git" / "config").read_text(), "the token is never persisted"
    assert await _git("remote", "get-url", "origin", cwd=repo) == _PRIVATE


async def test_no_token_means_no_publish(shallow_private, tmp_path: Path) -> None:
    store = LocalFilesystemBundleStore(tmp_path / "bundles")

    async def _none() -> str | None:
        return None

    outcome = await publish_product_bundle(shallow_private, store=store, credential=_none)

    assert outcome.published is False


async def test_a_full_repo_never_asks_for_the_credential(tmp_path: Path) -> None:
    from backend.storage.product_workspace import init_product_workspace

    product_id = uuid.uuid4()
    await init_product_workspace(product_id)
    store = LocalFilesystemBundleStore(tmp_path / "bundles")

    async def _boom() -> str | None:
        raise AssertionError("a full repo needs no credential")

    outcome = await publish_product_bundle(product_id, store=store, credential=_boom)

    assert outcome.published is True
