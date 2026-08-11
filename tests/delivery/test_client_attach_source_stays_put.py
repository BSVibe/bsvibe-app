"""The two places that would put a client_attach product's source on the server.

Resolving a github binding for such a product (#736 follow-up) re-opens every
path that binding feeds. Two of them fetch the repo onto this machine, and the
privacy contract (§3.5) says neither may:

* the run-setup **provisioner** clones the delivery target into the run's
  server-side workspace;
* the merge-watch **freshness** resolver hands the worker what it needs to
  re-clone a reaped workspace and merge base into the run branch locally — and
  auto-merge is ON in production, so this is a live path, not a hypothetical.

Auto-merging itself is untouched: that is an API call about a PR, and it needs
no source. What cannot happen is the source arriving here.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import ProductRow
from backend.workflow.application.delivery.connector_dispatch._github import (
    build_github_workspace_provisioner,
)
from backend.workflow.application.runtime.merge_watch_runtime import (
    build_merge_watch_freshness_resolver,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from tests._support import shared_file_sessionmaker

pytestmark = pytest.mark.asyncio


class _RoundtripCipher:
    def encrypt(self, plaintext: str) -> str:
        return f"ct:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext.removeprefix("ct:")


class _ForbiddenGitOps:
    async def clone(self, repo_url: str, dest: Path, *, token: str | None, depth: int = 1) -> None:
        raise AssertionError("the server must never clone a client_attach product's source")

    async def checkout_new_branch(self, dest: Path, branch: str) -> None:
        raise AssertionError("there is no server-side checkout to branch")


async def _seed(sf, cipher: _RoundtripCipher, *, execution_target: str):  # noqa: ANN202
    ws, product_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sf() as s:
        s.add(
            ConnectorAccountRow(
                workspace_id=ws,
                connector="github",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext=cipher.encrypt("tok"),
                delivery_config={"repo": "owner/name"},
                is_active=True,
            )
        )
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=ws,
                slug=f"p-{product_id.hex[:6]}",
                name="p",
                repo_url="https://github.com/owner/name",
                product_metadata={"execution_target": execution_target},
            )
        )
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                product_id=product_id,
                status=RunStatus.REVIEW_READY,
                payload={},
            )
        )
        await s.commit()
    return ws, product_id, run_id


async def test_the_provisioner_never_clones_a_client_attach_product(tmp_path: Path) -> None:
    """Its own guard, not one inherited from the composite provisioner above it.

    Defence belongs at the operation that is dangerous: a caller that reaches
    this function directly — or a future composite that forgets — must still
    find the clone refused.
    """
    cipher = _RoundtripCipher()
    async with shared_file_sessionmaker() as sf:
        ws, product_id, run_id = await _seed(sf, cipher, execution_target="client_attach")
        provision = build_github_workspace_provisioner(
            cipher=cipher,  # type: ignore[arg-type]
            git_ops=_ForbiddenGitOps(),  # type: ignore[arg-type]
            remote_url_for=lambda repo: f"https://example.invalid/{repo}.git",
        )
        run = SimpleNamespace(id=run_id, workspace_id=ws, product_id=product_id)
        workspace_dir = tmp_path / "ws"
        workspace_dir.mkdir()
        async with sf() as s:
            await provision(s, run, workspace_dir)

        assert not any(workspace_dir.iterdir()), "the run's server-side workspace must stay empty"


async def test_the_freshness_resolver_refuses_a_client_attach_run() -> None:
    """``None`` terminates the behind/dirty path before any git runs.

    The founder's machine holds the checkout, so freshening a stale PR has to
    happen there — a later lift. Until then this is an honest stop rather than a
    silent re-clone: the PR stays open and waits for a human.
    """
    cipher = _RoundtripCipher()
    async with shared_file_sessionmaker() as sf:
        ws, _product_id, run_id = await _seed(sf, cipher, execution_target="client_attach")
        resolve = build_merge_watch_freshness_resolver(cipher=cipher)  # type: ignore[arg-type]
        async with sf() as s:
            target = await resolve(s, ws, run_id)

    assert target is None


async def test_the_freshness_resolver_still_serves_a_server_sandbox_run() -> None:
    cipher = _RoundtripCipher()
    async with shared_file_sessionmaker() as sf:
        ws, _product_id, run_id = await _seed(sf, cipher, execution_target="server_sandbox")
        resolve = build_merge_watch_freshness_resolver(cipher=cipher)  # type: ignore[arg-type]
        async with sf() as s:
            target = await resolve(s, ws, run_id)

    assert target is not None
    assert target.repo == "owner/name"
