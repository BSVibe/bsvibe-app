"""Auto-ship gate — which runs ship by fast-forwarding the LOCAL product repo.

``_auto_ship_product_run`` runs the local ``merge_to_main`` fast-forward. That is
only valid for a run whose work lives in the product's own repo. A run in a
workspace with a **github delivery binding** was provisioned as a clone of the
github repo and delivers via the push+PR path instead (issue #362) — the local
fast-forward can only fail there, so the gate must skip it.

The gate used to infer this from the workspace's *filesystem shape* (``.git`` is
a pointer FILE → linked worktree → local; a DIRECTORY → clone → github). That
inference is an accident of today's provisioning: the moment local products are
also materialised as full clones (the R2-bundle migration), every local product
would silently stop auto-shipping. The gate therefore asks the SAME source the
provisioner branches on — the github binding — and only uses the filesystem to
answer "was a workspace provisioned at all".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import get_settings
from backend.storage.product_workspace import run_worktree_path
from backend.workflow.application.agent_runner import delivers_via_local_product_repo
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _isolate_run_root(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "run_workspace_root", str(tmp_path / "runs"), raising=False)


def _run(*, workspace_id: uuid.UUID, product_id: uuid.UUID | None) -> ExecutionRun:
    return ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=product_id,
        status=RunStatus.REVIEW_READY,
        payload={},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


def _provision(run: ExecutionRun, *, dotgit: str) -> None:
    """Create the run's workspace with ``.git`` as a ``file`` or a ``dir``."""
    path = run_worktree_path(run.id)
    path.mkdir(parents=True)
    if dotgit == "file":
        (path / ".git").write_text("gitdir: /app/var/products/p/.git/worktrees/r\n")
    else:
        (path / ".git").mkdir()


async def _seed_github_binding(sf, workspace_id: uuid.UUID) -> None:
    from backend.connectors.db import ConnectorAccountRow
    from backend.router.accounts.crypto import CredentialCipher

    cipher = CredentialCipher(b"0" * 32)
    async with sf() as s:
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                connector="github",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext=cipher.encrypt("ghp_test_token"),
                delivery_config={"repo": "owner/name", "base_branch": "main"},
                is_active=True,
            )
        )
        await s.commit()


async def test_local_product_run_with_linked_worktree_ships_locally(sf) -> None:
    """Today's local shape: a linked worktree, no github binding."""
    run = _run(workspace_id=uuid.uuid4(), product_id=uuid.uuid4())
    _provision(run, dotgit="file")
    async with sf() as s:
        assert await delivers_via_local_product_repo(s, run) is True


async def test_local_product_run_with_full_clone_still_ships_locally(sf) -> None:
    """THE MIGRATION-CRITICAL CASE: after local products are materialised from an
    R2 bundle their workspace is a full clone (``.git`` is a DIRECTORY). The old
    filesystem-shape gate returned False here, which would have silently stopped
    auto-shipping every local product. Ownership is decided by the binding, not
    the on-disk shape."""
    run = _run(workspace_id=uuid.uuid4(), product_id=uuid.uuid4())
    _provision(run, dotgit="dir")
    async with sf() as s:
        assert await delivers_via_local_product_repo(s, run) is True


async def test_github_bound_run_does_not_ship_locally(sf) -> None:
    """A github-bound workspace delivers via push+PR — the local fast-forward
    would only fail. True even when the workspace looks linked on disk."""
    ws = uuid.uuid4()
    await _seed_github_binding(sf, ws)
    run = _run(workspace_id=ws, product_id=uuid.uuid4())
    _provision(run, dotgit="file")
    async with sf() as s:
        assert await delivers_via_local_product_repo(s, run) is False


async def test_run_without_product_does_not_ship_locally(sf) -> None:
    run = _run(workspace_id=uuid.uuid4(), product_id=None)
    _provision(run, dotgit="file")
    async with sf() as s:
        assert await delivers_via_local_product_repo(s, run) is False


async def test_unprovisioned_workspace_does_not_ship_locally(sf) -> None:
    """Glue tests that bypass the provisioner leave no ``.git`` at all — the run
    stays at REVIEW_READY exactly as before (the pre-W2 invariant)."""
    run = _run(workspace_id=uuid.uuid4(), product_id=uuid.uuid4())
    run_worktree_path(run.id).mkdir(parents=True)  # dir, but no .git
    async with sf() as s:
        assert await delivers_via_local_product_repo(s, run) is False
