"""GitHub run-workspace provisioner idempotency under drive_once re-entry.

``AgentWorker.drive_once`` re-enters ``_frame_and_drive`` (and thus the github
``_provision`` closure) every time a paused run is RESUMED (a resolved Decision
transitions the run RUNNING → OPEN). ``_provision`` performs ONE-TIME setup —
``git clone`` the target repo into ``workspace_dir`` — which is NOT idempotent:
``git clone`` refuses a non-empty destination. On resume the workspace_dir
already holds the prior checkout, so a naive re-clone raises ``GitError``, the
tick rolls back, and the run stalls OPEN forever.

These tests pin the fix: when the run's workspace already holds a checkout (a
``.git`` dir), ``_provision`` REUSES it (no second clone) instead of re-cloning.
Reuse is not merely idempotent — it is semantically correct: a fresh clone would
discard any work the agent did before the pause and would drop the run branch.

A counting fake GitOps stands in for real git so the tests assert on the clone
count directly (no real remote needed).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.application.delivery.connector_dispatch import (
    build_github_workspace_provisioner,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import db_engine

TEST_KEY = b"0123456789abcdef0123456789abcdef"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


class _CountingGitOps:
    """A fake ``GitOps`` that counts clones and materialises a ``.git`` checkout
    exactly as a real clone would — so the second ``_provision`` sees the
    resume condition (a non-empty dir with a checkout)."""

    def __init__(self) -> None:
        self.clone_calls = 0
        self.branch_calls = 0

    async def clone(self, repo_url: str, dest: Path, *, token: str | None, depth: int = 1) -> None:
        self.clone_calls += 1
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".git").mkdir()

    async def checkout_new_branch(self, dest: Path, branch: str) -> None:
        self.branch_calls += 1


async def _seed_github_connector(
    session: AsyncSession, cipher: CredentialCipher, workspace_id: uuid.UUID
) -> None:
    session.add(
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
    await session.commit()


def _make_run(workspace_id: uuid.UUID) -> ExecutionRun:
    return ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        request_id=None,
        status=RunStatus.OPEN,
        payload={},
    )


async def test_provision_reuses_existing_checkout_on_second_call(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher, tmp_path: Path
) -> None:
    """Calling ``_provision`` TWICE against the same workspace_dir clones once:
    the first call clones (fresh empty dir), the second sees the ``.git``
    checkout and REUSES it — no second clone, no GitError."""
    workspace_id = uuid.uuid4()
    async with sf() as s:
        await _seed_github_connector(s, cipher, workspace_id)

    ops = _CountingGitOps()
    provision = build_github_workspace_provisioner(cipher=cipher, git_ops=ops)

    run = _make_run(workspace_id)
    workspace_dir = tmp_path / str(run.id)
    workspace_dir.mkdir(parents=True)  # AgentWorker creates an empty dir first

    async with sf() as s:
        await provision(s, run, workspace_dir)
        # Second drive re-enters with the SAME (now non-empty) workspace_dir.
        await provision(s, run, workspace_dir)

    assert ops.clone_calls == 1, "resume must reuse the checkout, not re-clone"
    assert ops.branch_calls == 1, "the run branch is created once, at first clone"
    assert (workspace_dir / ".git").exists()


async def test_provision_reuses_when_checkout_preexists(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher, tmp_path: Path
) -> None:
    """Simulate the prod resume condition directly: a workspace_dir that already
    holds a checkout (``.git``). A single ``_provision`` call must NOT clone and
    must return cleanly (the prod GitError path is closed)."""
    workspace_id = uuid.uuid4()
    async with sf() as s:
        await _seed_github_connector(s, cipher, workspace_id)

    ops = _CountingGitOps()
    provision = build_github_workspace_provisioner(cipher=cipher, git_ops=ops)

    run = _make_run(workspace_id)
    workspace_dir = tmp_path / str(run.id)
    workspace_dir.mkdir(parents=True)
    (workspace_dir / ".git").mkdir()  # a prior drive already provisioned this

    async with sf() as s:
        await provision(s, run, workspace_dir)  # must not raise

    assert ops.clone_calls == 0, "a pre-existing checkout is reused, never re-cloned"


async def test_provision_clones_on_fresh_empty_dir(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher, tmp_path: Path
) -> None:
    """The fresh-run case is unchanged: an empty workspace_dir clones exactly
    once (the reuse guard must not suppress the first, legitimate clone)."""
    workspace_id = uuid.uuid4()
    async with sf() as s:
        await _seed_github_connector(s, cipher, workspace_id)

    ops = _CountingGitOps()
    provision = build_github_workspace_provisioner(cipher=cipher, git_ops=ops)

    run = _make_run(workspace_id)
    workspace_dir = tmp_path / str(run.id)
    workspace_dir.mkdir(parents=True)

    async with sf() as s:
        await provision(s, run, workspace_dir)

    assert ops.clone_calls == 1
    assert ops.branch_calls == 1
