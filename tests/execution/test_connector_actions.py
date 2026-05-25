"""ConnectorActionResolver unit tests (B5b).

The resolver is the production :class:`ConnectorActionProvider` the worker
factory threads into the native loop. These prove the three resolver duties in
isolation (the orchestrator tests stub the provider): listing the workspace's
``mcp_exposed`` connector actions, decrypting account credentials, and
dispatching the action with the credential-bearing context.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from backend.accounts.crypto import CredentialCipher
from backend.connectors.db import ConnectorAccountRow
from backend.execution.connector_actions import (
    ConnectorActionProvider,
    ConnectorActionResolver,
    ConnectorActionTool,
    loop_tool_name,
)
from backend.plugins.base import ActionCapability, PluginMeta

from .._support import memory_session


def _cipher() -> CredentialCipher:
    return CredentialCipher(os.urandom(32))


def _plugin(name: str, *, actions: dict[str, ActionCapability] | None = None) -> PluginMeta:
    return PluginMeta(
        name=name,
        version="0",
        description="",
        author="t",
        data_jurisdiction="us",
        credentials=[],
        actions=actions or {},
    )


def _action(name: str, *, mcp_exposed: bool = True) -> ActionCapability:
    recorded: dict[str, Any] = {}

    async def _fn(context: Any, **kwargs: Any) -> dict[str, Any]:
        recorded["context"] = context
        recorded["kwargs"] = kwargs
        return {"ran": name, "kwargs": kwargs}

    cap = ActionCapability(fn=_fn, name=name, mcp_exposed=mcp_exposed, input_schema=None)
    cap._recorded = recorded  # type: ignore[attr-defined]  # test introspection handle
    return cap


async def _seed_account(
    session, *, workspace_id: uuid.UUID, connector: str, cipher: CredentialCipher, secret: str
) -> ConnectorAccountRow:
    row = ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        connector=connector,
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext=cipher.encrypt(secret),
        delivery_config={"repo": "owner/name"},
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return row


def test_resolver_satisfies_provider_protocol() -> None:
    assert isinstance(
        ConnectorActionResolver(
            session=object(),  # type: ignore[arg-type]
            plugins_by_name={},
            danger_map={},
            cipher=_cipher(),
        ),
        ConnectorActionProvider,
    )


def test_loop_tool_name_namespaces_connector_and_action() -> None:
    assert loop_tool_name("github", "open_pr") == "github__open_pr"


async def test_list_actions_only_for_workspace_accounts() -> None:
    cipher = _cipher()
    ws = uuid.uuid4()
    plugins = {"github": _plugin("github", actions={"open_pr": _action("open_pr")})}
    async with memory_session() as session:
        await _seed_account(
            session, workspace_id=ws, connector="github", cipher=cipher, secret="ghp"
        )
        resolver = ConnectorActionResolver(
            session=session, plugins_by_name=plugins, danger_map={"github": True}, cipher=cipher
        )
        tools = await resolver.list_actions(ws)
        assert len(tools) == 1
        assert tools[0].connector == "github"
        assert tools[0].action_name == "open_pr"
        assert tools[0].is_dangerous is True
        # A different workspace with no account → no actions.
        assert await resolver.list_actions(uuid.uuid4()) == []


async def test_list_actions_skips_non_mcp_exposed_and_unloaded() -> None:
    cipher = _cipher()
    ws = uuid.uuid4()
    plugins = {
        "github": _plugin(
            "github",
            actions={
                "open_pr": _action("open_pr", mcp_exposed=True),
                "internal_op": _action("internal_op", mcp_exposed=False),
            },
        )
    }
    async with memory_session() as session:
        await _seed_account(
            session, workspace_id=ws, connector="github", cipher=cipher, secret="ghp"
        )
        # An account whose connector has no loaded plugin → skipped.
        await _seed_account(
            session, workspace_id=ws, connector="unknown", cipher=cipher, secret="x"
        )
        resolver = ConnectorActionResolver(
            session=session, plugins_by_name=plugins, danger_map={}, cipher=cipher
        )
        tools = await resolver.list_actions(ws)
        names = {t.action_name for t in tools}
        assert names == {"open_pr"}  # non-exposed + unloaded both excluded
        # No danger_map entry → defaults to not dangerous.
        assert tools[0].is_dangerous is False


async def test_list_actions_skips_inactive_account() -> None:
    cipher = _cipher()
    ws = uuid.uuid4()
    plugins = {"github": _plugin("github", actions={"open_pr": _action("open_pr")})}
    async with memory_session() as session:
        row = await _seed_account(
            session, workspace_id=ws, connector="github", cipher=cipher, secret="ghp"
        )
        row.is_active = False
        await session.flush()
        resolver = ConnectorActionResolver(
            session=session, plugins_by_name=plugins, danger_map={}, cipher=cipher
        )
        assert await resolver.list_actions(ws) == []


async def test_credentials_for_decrypts_account_secret() -> None:
    cipher = _cipher()
    ws = uuid.uuid4()
    plugins = {"github": _plugin("github", actions={"open_pr": _action("open_pr")})}
    async with memory_session() as session:
        await _seed_account(
            session, workspace_id=ws, connector="github", cipher=cipher, secret="super-secret"
        )
        resolver = ConnectorActionResolver(
            session=session, plugins_by_name=plugins, danger_map={}, cipher=cipher
        )
        tool = (await resolver.list_actions(ws))[0]
        creds = resolver.credentials_for(tool)
        assert creds == {"token": "super-secret"}


async def test_dispatch_injects_credentials_into_action_context() -> None:
    cipher = _cipher()
    ws = uuid.uuid4()
    action = _action("open_pr")
    plugins = {"github": _plugin("github", actions={"open_pr": action})}
    async with memory_session() as session:
        await _seed_account(
            session, workspace_id=ws, connector="github", cipher=cipher, secret="tkn"
        )
        resolver = ConnectorActionResolver(
            session=session, plugins_by_name=plugins, danger_map={}, cipher=cipher
        )
        tool = (await resolver.list_actions(ws))[0]
        creds = resolver.credentials_for(tool)
        result = await resolver.dispatch(tool, credentials=creds, kwargs={"title": "T"})
        assert result == {"ran": "open_pr", "kwargs": {"title": "T"}}
        # The plugin fn saw the decrypted credentials + the founder-set config.
        recorded = action._recorded  # type: ignore[attr-defined]
        assert recorded["context"].credentials == {"token": "tkn"}
        assert recorded["context"].config == {"repo": "owner/name"}
        assert recorded["kwargs"] == {"title": "T"}


def test_tool_is_frozen_dataclass() -> None:
    """ConnectorActionTool is hashable/immutable so handlers can capture it."""
    plugin = _plugin("github", actions={"open_pr": _action("open_pr")})
    account = ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        connector="github",
        webhook_token="t",
        signing_secret_ciphertext="x",
        delivery_config={},
        is_active=True,
    )
    tool = ConnectorActionTool(
        plugin=plugin,
        action=plugin.actions["open_pr"],
        account=account,
        is_dangerous=False,
    )
    assert tool.connector == "github"
    assert tool.action_name == "open_pr"
