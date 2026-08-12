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
from backend.workflow.application.runtime.merge_watch_freshen import build_branch_freshener
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


async def test_the_freshen_never_takes_a_client_attach_run_down_the_server_git() -> None:
    """The freshen happens where the checkout is (#742 follow-up).

    Freshening a stale PR means merging base into the run branch and pushing —
    which the server can only do by holding the source. For a client_attach
    product that is the one thing this contract forbids, so the picker routes it
    to the founder's machine instead. Here no machine is reachable (no redis),
    which must come back as "could not freshen" and NOT as a server-side clone.
    """
    cipher = _RoundtripCipher()
    async with shared_file_sessionmaker() as sf:
        ws, _product_id, run_id = await _seed(sf, cipher, execution_target="client_attach")
        freshen = build_branch_freshener(
            cipher=cipher,
            session_factory=sf,
            redis_client=None,
            run_workspace_root=Path("/nonexistent"),
            git_ops=_ForbiddenGitOps(),
        )
        async with sf() as s:
            outcome = await freshen(s, ws, run_id, "run/abc")

    assert outcome.status == "failed"


async def test_the_freshen_still_serves_a_server_sandbox_run() -> None:
    """The other model is untouched: it resolves its binding and uses the
    server-side git exactly as before."""
    cipher = _RoundtripCipher()
    seen: dict[str, object] = {}

    class _Git(_ForbiddenGitOps):
        async def clone(
            self, repo_url: str, dest: Path, *, token: str | None, depth: int = 1
        ) -> None:
            seen["cloned"] = repo_url

        async def checkout(self, dest: Path, branch: str) -> None:
            return None

        async def fetch(self, *args: object, **kwargs: object) -> None:
            return None

        async def merge_ref(self, *args: object, **kwargs: object) -> object:
            return SimpleNamespace(status="clean", conflict_paths=[])

        async def push(self, *args: object, **kwargs: object) -> None:
            seen["pushed"] = True

    async with shared_file_sessionmaker() as sf:
        ws, _product_id, run_id = await _seed(sf, cipher, execution_target="server_sandbox")
        freshen = build_branch_freshener(
            cipher=cipher,
            session_factory=sf,
            redis_client=None,
            run_workspace_root=Path("/tmp/merge-watch-freshen-test"),  # noqa: S108 — never written
            git_ops=_Git(),
        )
        async with sf() as s:
            outcome = await freshen(s, ws, run_id, "run/abc")

    assert outcome.status == "clean"
    assert seen.get("pushed") is True
