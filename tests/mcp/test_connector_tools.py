"""Connector MCP tool tests — PWA Settings → Connectors parity (real DB)."""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import get_settings
from backend.connectors.auth import providers as providers_mod
from backend.connectors.auth import store
from backend.connectors.auth.github import GitHubAppProvider
from backend.connectors.auth.providers import register_provider
from backend.connectors.auth.tokenset import TokenSet
from backend.connectors.db import ConnectorAccountRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings

from .._support import db_engine


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator[async_sessionmaker]:
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    snapshot = dict(providers_mod._REGISTRY)
    try:
        yield
    finally:
        providers_mod._REGISTRY.clear()
        providers_mod._REGISTRY.update(snapshot)


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


def _principal(workspace_id: uuid.UUID, *, scopes: tuple[str, ...]) -> McpPrincipal:
    return McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset(scopes),
        jti=uuid.uuid4(),
    )


def test_registers_six_connector_tools(registry: ToolRegistry) -> None:
    names = set(registry.names())
    assert {
        "bsvibe_connector_list",
        "bsvibe_connector_create",
        "bsvibe_connector_revoke",
        "bsvibe_connector_oauth_start",
        "bsvibe_connector_github_app_status",
        "bsvibe_connector_github_app_setup_url",
    } <= names


async def test_create_then_list_shows_connector(db, workspace_id, registry: ToolRegistry) -> None:
    async with db() as s:
        ctx = ToolContext(principal=_principal(workspace_id, scopes=("mcp:write",)), session=s)
        out = await registry.call_tool(
            "bsvibe_connector_create",
            {"connector": "telegram", "signing_secret": "shh", "delivery_config": {"chat_id": "1"}},
            ctx,
        )
    assert out["connector"] == "telegram"
    assert out["webhook_url"] == f"/api/webhooks/telegram/{out['webhook_token']}"

    async with db() as s:
        ctx = ToolContext(principal=_principal(workspace_id, scopes=("mcp:read",)), session=s)
        listed = await registry.call_tool("bsvibe_connector_list", {}, ctx)
    assert any(c["connector"] == "telegram" and c["is_active"] for c in listed["connectors"])


async def test_create_rejects_unknown_connector(db, workspace_id, registry: ToolRegistry) -> None:
    async with db() as s:
        ctx = ToolContext(principal=_principal(workspace_id, scopes=("mcp:write",)), session=s)
        with pytest.raises(ToolError, match="unknown connector"):
            await registry.call_tool(
                "bsvibe_connector_create",
                {"connector": "pager-duty", "signing_secret": "x"},
                ctx,
            )


async def test_list_surfaces_oauth_account_label(db, workspace_id, registry: ToolRegistry) -> None:
    cipher = CredentialCipher(_key_from_settings())
    async with db() as s:
        account = ConnectorAccountRow(
            workspace_id=workspace_id,
            connector="github",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("no-webhook-secret"),
            delivery_config={},
            is_active=True,
        )
        s.add(account)
        await s.flush()
        await store.upsert_token(
            s,
            connector_account_id=account.id,
            provider="github",
            token=TokenSet(access_token="ghu", account_label="@octocat"),
            cipher=cipher,
        )
        await s.commit()
        aid = str(account.id)

    async with db() as s:
        ctx = ToolContext(principal=_principal(workspace_id, scopes=("mcp:read",)), session=s)
        listed = await registry.call_tool("bsvibe_connector_list", {}, ctx)
    item = next(c for c in listed["connectors"] if c["id"] == aid)
    assert item["oauth_account_label"] == "@octocat"


async def test_revoke_flips_inactive(db, workspace_id, registry: ToolRegistry) -> None:
    cipher = CredentialCipher(_key_from_settings())
    async with db() as s:
        account = ConnectorAccountRow(
            workspace_id=workspace_id,
            connector="telegram",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("x"),
            delivery_config={},
            is_active=True,
        )
        s.add(account)
        await s.commit()
        aid = str(account.id)

    async with db() as s:
        ctx = ToolContext(principal=_principal(workspace_id, scopes=("mcp:write",)), session=s)
        out = await registry.call_tool("bsvibe_connector_revoke", {"connector_id": aid}, ctx)
    assert out["revoked"] is True

    async with db() as s:
        row = await s.get(ConnectorAccountRow, uuid.UUID(aid))
        assert row is not None and row.is_active is False


async def test_oauth_start_unknown_provider_errors(
    db, workspace_id, registry: ToolRegistry
) -> None:
    async with db() as s:
        ctx = ToolContext(principal=_principal(workspace_id, scopes=("mcp:write",)), session=s)
        with pytest.raises(ToolError, match="not configured"):
            await registry.call_tool("bsvibe_connector_oauth_start", {"provider": "github"}, ctx)


async def test_oauth_start_returns_authorize_url(db, workspace_id, registry: ToolRegistry) -> None:
    register_provider(GitHubAppProvider(client_id="Iv1.x", client_secret="s"))  # noqa: S106
    async with db() as s:
        ctx = ToolContext(principal=_principal(workspace_id, scopes=("mcp:write",)), session=s)
        out = await registry.call_tool("bsvibe_connector_oauth_start", {"provider": "github"}, ctx)
    assert out["authorize_url"].startswith("https://github.com/login/oauth/authorize")
    assert "instructions" in out


async def test_github_app_status_not_configured(db, workspace_id, registry: ToolRegistry) -> None:
    async with db() as s:
        ctx = ToolContext(principal=_principal(workspace_id, scopes=("mcp:read",)), session=s)
        out = await registry.call_tool("bsvibe_connector_github_app_status", {}, ctx)
    assert out["configured"] is False


async def test_github_app_setup_url_returns_manifest(
    db, workspace_id, registry: ToolRegistry
) -> None:
    async with db() as s:
        ctx = ToolContext(principal=_principal(workspace_id, scopes=("mcp:write",)), session=s)
        out = await registry.call_tool("bsvibe_connector_github_app_setup_url", {}, ctx)
    assert out["post_url"].startswith("https://github.com/settings/apps/new")
    assert "redirect_url" in out["manifest"]


async def test_write_tool_denied_for_read_only_principal(
    db, workspace_id, registry: ToolRegistry
) -> None:
    async with db() as s:
        ctx = ToolContext(principal=_principal(workspace_id, scopes=("mcp:read",)), session=s)
        with pytest.raises(ToolError):
            await registry.call_tool(
                "bsvibe_connector_create",
                {"connector": "telegram", "signing_secret": "x"},
                ctx,
            )
