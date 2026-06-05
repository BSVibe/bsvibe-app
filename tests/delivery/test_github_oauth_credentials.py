"""github delivery uses the OAuth token, not the legacy signing secret (Lift 1).

Before Lift 1 the github delivery path (clone / push / open_pr) decrypted
``connector_accounts.signing_secret_ciphertext`` directly — so "Connect with
GitHub" would store an OAuth token that delivery never used (it kept pushing
with the placeholder secret). These tests pin the re-wire: every github
credential now flows through ``resolve_connector_credentials``, so an OAuth
token row takes precedence over the legacy secret.

Real DB (shared file SQLite), no dependency_overrides — the credential
resolution is exercised through the actual store + resolve layer (skill
mock-fixtures-hide-wiring-bugs).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.connectors.auth import store
from backend.connectors.auth.tokenset import TokenSet
from backend.connectors.db import ConnectorAccountRow
from backend.workflow.application.delivery.connector_dispatch._github import (
    GithubDeliveryDeps,
    build_github_workspace_provisioner,
    deliver_github,
)
from backend.workflow.application.delivery.connector_dispatch._resolver import GithubBinding
from tests._support import shared_file_sessionmaker

pytestmark = pytest.mark.asyncio

_OAUTH_TOKEN = "ghu_oauth_access"  # noqa: S105 — test fixture
_LEGACY_SECRET = "ghp_legacy_pat"  # noqa: S105 — test fixture


class _RoundtripCipher:
    """A reversible stand-in for CredentialCipher (no real crypto needed)."""

    def encrypt(self, plaintext: str) -> str:
        return f"ct:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext.removeprefix("ct:")


@dataclass
class _CapturingGitOps:
    clone_token: str | None = None
    push_token: str | None = None
    committed: bool = True

    async def clone(self, repo_url: str, dest: Path, *, token: str | None, depth: int = 1) -> None:
        self.clone_token = token

    async def checkout_new_branch(self, dest: Path, branch: str) -> None:
        return None

    async def commit_all(self, dest: Path, message: str) -> bool:
        return self.committed

    async def push(self, dest: Path, branch: str, *, token: str | None) -> None:
        self.push_token = token


@dataclass
class _CapturingRunner:
    pr_token: str | None = None

    async def dispatch_action(self, plugin, *, action_name, context, kwargs):  # type: ignore[no-untyped-def]
        self.pr_token = context.credentials.get("token")
        return {"url": "https://github.com/owner/name/pull/1"}


async def _seed_github_with_oauth(
    sf, cipher: _RoundtripCipher
) -> tuple[uuid.UUID, ConnectorAccountRow]:
    ws = uuid.uuid4()
    async with sf() as s:
        account = ConnectorAccountRow(
            workspace_id=ws,
            connector="github",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt(_LEGACY_SECRET),
            delivery_config={"repo": "owner/name"},
            is_active=True,
        )
        s.add(account)
        await s.flush()
        await store.upsert_token(
            s,
            connector_account_id=account.id,
            provider="github",
            token=TokenSet(access_token=_OAUTH_TOKEN, refresh_token=None, expires_at=None),
            cipher=cipher,  # type: ignore[arg-type]
        )
        await s.commit()
        # Detach the account (mirrors dispatch resolving the binding in a
        # session that closes before delivery runs).
        s.expunge(account)
    return ws, account


async def test_provisioner_clones_with_oauth_token(tmp_path: Path) -> None:
    cipher = _RoundtripCipher()
    async with shared_file_sessionmaker() as sf:
        ws, account = await _seed_github_with_oauth(sf, cipher)
        git_ops = _CapturingGitOps()
        provision = build_github_workspace_provisioner(
            cipher=cipher,  # type: ignore[arg-type]
            git_ops=git_ops,  # type: ignore[arg-type]
            remote_url_for=lambda repo: f"https://example.invalid/{repo}.git",
        )
        run = SimpleNamespace(id=uuid.uuid4(), workspace_id=ws)
        workspace_dir = tmp_path / "ws"
        workspace_dir.mkdir()
        async with sf() as s:
            await provision(s, run, workspace_dir)

    assert git_ops.clone_token == _OAUTH_TOKEN


async def test_deliver_github_pushes_and_opens_pr_with_oauth_token(tmp_path: Path) -> None:
    cipher = _RoundtripCipher()
    async with shared_file_sessionmaker() as sf:
        ws, account = await _seed_github_with_oauth(sf, cipher)
        run_id = uuid.uuid4()
        checkout = tmp_path / str(run_id)
        checkout.mkdir()
        git_ops = _CapturingGitOps()
        runner = _CapturingRunner()
        deps = GithubDeliveryDeps(
            cipher=cipher,  # type: ignore[arg-type]
            plugins_by_name={"github": object()},  # type: ignore[dict-item]
            workspace_root=tmp_path,
            git_ops=git_ops,  # type: ignore[arg-type]
            remote_url_for=lambda repo: f"https://example.invalid/{repo}.git",
            runner=runner,  # type: ignore[arg-type]
            session_factory=sf,
        )
        binding = GithubBinding(account=account, repo="owner/name", base_branch="main")
        actions = await deliver_github(
            deps=deps,
            binding=binding,
            workspace_id=ws,
            deliverable_id=uuid.uuid4(),
            run_id=run_id,
            content={"summary": "Title\n\nBody"},
        )

    assert actions[0].succeeded is True
    assert git_ops.push_token == _OAUTH_TOKEN
    assert runner.pr_token == _OAUTH_TOKEN
