"""Connector ``@p.action`` → agent-loop tool bridge (RC-1, part 2 / B5b).

A prior audit found a gap: connectors declare ``@p.action`` (github
``open_pr``, notion ``create_page``, slack ``post_message`` …) with
``mcp_exposed=True``, but the NATIVE agent loop
(:class:`~backend.execution.orchestrator.RunOrchestrator`) never folded them
into its tool set — so the work LLM could edit files + run shell yet could NOT
take connector actions mid-run.

This module is the seam that closes it. It mirrors the B5a knowledge/skill
extension point: a single injected provider the orchestrator depends on (one
seam, never a Union of concretes — per the ``bsvibe-llm-wrapper-not-raw-litellm``
rule). The provider:

1. **Lists** the workspace's available connector actions — the ``mcp_exposed``
   actions of plugins for which the workspace has an active
   :class:`~backend.connectors.db.ConnectorAccountRow`. A workspace with no
   connector accounts → no actions (the orchestrator registers no connector
   tools, zero behaviour change).
2. **Resolves + decrypts** the per-account credential into the action's
   :class:`~backend.extensions.plugin.context.SkillContext`.
3. **Dispatches** the action through
   :meth:`~backend.extensions.plugin.runner.PluginRunner.dispatch_action`.

Lift 0c (YAGNI rollback) removed the static plugin-load DangerAnalyzer + the
``is_dangerous`` flag it produced + the pre-M2 ``tool.is_dangerous and
safe_mode`` gate that was its only consumer. Workspace ``safe_mode`` is
preserved as a workspace-level setting, but per-call danger gating is gone —
re-introduce it from a real producer (a manual ``@p.action(dangerous=True)``
opt-in) when there is a concrete need.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.base import ActionCapability, PluginMeta
from backend.extensions.plugin.context import SkillContext
from backend.extensions.plugin.runner import PluginRunner
from backend.router.accounts.crypto import CredentialCipher

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ConnectorActionTool:
    """One connector action the work LLM may call mid-run.

    ``account`` is the workspace's active :class:`ConnectorAccountRow` for this
    connector (its encrypted secret is what gets decrypted into the action
    context).
    """

    plugin: PluginMeta
    action: ActionCapability
    account: ConnectorAccountRow

    @property
    def connector(self) -> str:
        return self.plugin.name

    @property
    def action_name(self) -> str:
        return self.action.name


@runtime_checkable
class ConnectorActionProvider(Protocol):
    """The single seam the orchestrator depends on for connector actions.

    The production resolver (:class:`ConnectorActionResolver`) satisfies it
    structurally; tests inject a fake. ``None`` in the orchestrator means no
    connector tools (back-compat)."""

    async def list_actions(self, workspace_id: uuid.UUID) -> list[ConnectorActionTool]: ...

    def credentials_for(self, tool: ConnectorActionTool) -> dict[str, Any]: ...

    async def dispatch(
        self, tool: ConnectorActionTool, *, credentials: dict[str, Any], kwargs: dict[str, Any]
    ) -> Any: ...


# The credential slot the decrypted per-account secret is injected under for an
# agent-loop action call. A ``connector_account`` stores exactly one encrypted
# secret (``signing_secret_ciphertext``); the connectors that expose actions
# (github ``open_pr``/``comment``) read it as ``token``. Outbound delivery's
# per-connector ``credential_key`` mapping (notion ``token``, slack
# ``bot_token`` …) lives with the delivery event-builders; actions reuse the
# token slot the action-bearing connectors read.
_ACTION_CREDENTIAL_KEY = "token"


class ConnectorActionResolver:
    """Production :class:`ConnectorActionProvider`.

    Built per run by the worker factory with the run's session, the loaded
    plugin registry, and the settings-derived cipher.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        plugins_by_name: dict[str, PluginMeta],
        cipher: CredentialCipher,
    ) -> None:
        self._session = session
        self._plugins_by_name = plugins_by_name
        self._cipher = cipher
        self._runner = PluginRunner()

    async def list_actions(self, workspace_id: uuid.UUID) -> list[ConnectorActionTool]:
        """The workspace's available connector actions.

        An action qualifies when: its plugin is loaded, the action is
        ``mcp_exposed``, AND the workspace has an active connector account for
        that plugin. No accounts → empty list (the orchestrator surfaces no
        connector tools)."""
        rows = (
            (
                await self._session.execute(
                    select(ConnectorAccountRow).where(
                        ConnectorAccountRow.workspace_id == workspace_id,
                        ConnectorAccountRow.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        tools: list[ConnectorActionTool] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            plugin = self._plugins_by_name.get(row.connector)
            if plugin is None:
                continue
            for action in plugin.actions.values():
                if not action.mcp_exposed:
                    continue
                key = (plugin.name, action.name)
                if key in seen:
                    continue
                seen.add(key)
                tools.append(
                    ConnectorActionTool(
                        plugin=plugin,
                        action=action,
                        account=row,
                    )
                )
        return tools

    def credentials_for(self, tool: ConnectorActionTool) -> dict[str, Any]:
        """Decrypt the account's stored secret into the action credential slot."""
        secret = self._cipher.decrypt(tool.account.signing_secret_ciphertext)
        return {_ACTION_CREDENTIAL_KEY: secret}

    async def dispatch(
        self, tool: ConnectorActionTool, *, credentials: dict[str, Any], kwargs: dict[str, Any]
    ) -> Any:
        """Run the action through :class:`PluginRunner` with a credential-bearing
        :class:`SkillContext`. The context's ``config`` is the founder-set
        ``delivery_config`` (routing / non-secret fields), never work output."""
        context = SkillContext(
            llm=_NoLlm(),
            config=dict(tool.account.delivery_config or {}),
            logger=logger,
            credentials=credentials,
        )
        return await self._runner.dispatch_action(
            tool.plugin,
            action_name=tool.action_name,
            context=context,
            kwargs=kwargs,
        )


class _NoLlm:
    """A no-op LLM for the connector-action SkillContext.

    A connector action is a single external call (the agent loop already drives
    the planning), so it must not re-enter the LLM. :class:`SkillContext`
    requires a non-None ``llm``; calling it is a bug, so it raises."""

    async def chat(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("connector action must not call the LLM")


def loop_tool_name(connector: str, action: str) -> str:
    """The surfaced loop-tool name for a connector action.

    Namespaced ``<connector>__<action>`` so a connector action never collides
    with the built-in file/shell/verify tools (or another connector's action of
    the same name)."""
    return f"{connector}__{action}"


__all__ = [
    "ConnectorActionProvider",
    "ConnectorActionResolver",
    "ConnectorActionTool",
    "loop_tool_name",
]
