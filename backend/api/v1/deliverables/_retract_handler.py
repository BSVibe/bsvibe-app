"""Plugin-side runtime for B12b retract (Lift §17.9 sub-file).

Keeps the endpoint module (:mod:`.retract`) under the D35 thin-adapter
LOC ceiling by hosting the :class:`RetractHandler` protocol + production
implementation (:class:`PluginRetractHandler`) + dependency factory
(:func:`get_retract_handler`) here. The endpoint module only orchestrates
parse → handler dispatch → response.

Production wiring: loads the plugin registry, resolves the workspace's
``connector_account`` row for the named plugin, decrypts its secret, and
dispatches through :class:`PluginRunner` mirroring the delivery-time
SkillContext. Tests override :func:`get_retract_handler` with an in-test
stub so a unit run never touches the loader / KMS.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.base import PluginMeta, PluginRunError
from backend.extensions.plugin.context import SkillContext
from backend.extensions.plugin.runner import PluginRunner
from backend.router.accounts.crypto import CredentialCipher

logger = structlog.get_logger(__name__)


class RetractHandler(Protocol):
    """The runtime hand-off that actually calls a plugin's ``@p.compensate``.

    Stubbed in tests via the :func:`get_retract_handler` dependency override;
    production wires :class:`PluginRetractHandler` which loads the plugin
    registry, resolves the workspace's connector account for the named plugin,
    decrypts its secret, and dispatches through :class:`PluginRunner`.
    """

    async def compensate(
        self,
        *,
        plugin: str,
        artifact_type: str,
        handle: dict[str, Any],
        workspace_id: uuid.UUID,
    ) -> dict[str, Any]: ...


class _NoLlm:
    """A no-op LLM for the compensate :class:`SkillContext`.

    Compensation handlers call external APIs to revert artifacts — they should
    never invoke the LLM. Calling this raises rather than silently no-opping.
    Mirrors :class:`backend.workflow.application.delivery.connector_dispatch._NoLlm`.
    """

    async def chat(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("compensate must not call the LLM")


class PluginRetractHandler:
    """Production :class:`RetractHandler` — dispatches through the plugin runner.

    Per stored entry the handler:

    1. Looks up the plugin by name in the loaded registry.
    2. Resolves the workspace's active ``connector_account`` for that plugin
       (the same row the delivery used), decrypts its secret, builds a
       :class:`SkillContext` mirroring the delivery-time one.
    3. Calls :meth:`PluginRunner.dispatch_compensate` with the captured handle.

    Plugin or connector_account missing → :class:`PluginRunError` (the endpoint
    surfaces this as 502, the row is NOT marked retracted so the operator can
    see + retry). Handlers are idempotent (Workflow §9), so a stale handle
    yielding "already gone" is reported as success.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        plugins_by_name: dict[str, PluginMeta],
        cipher: CredentialCipher,
        runner: PluginRunner | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._plugins_by_name = plugins_by_name
        self._cipher = cipher
        self._runner = runner or PluginRunner()

    async def compensate(
        self,
        *,
        plugin: str,
        artifact_type: str,
        handle: dict[str, Any],
        workspace_id: uuid.UUID,
    ) -> dict[str, Any]:
        meta = self._plugins_by_name.get(plugin)
        if meta is None:
            raise PluginRunError(f"compensate: plugin {plugin!r} not loaded")
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ConnectorAccountRow).where(
                            ConnectorAccountRow.workspace_id == workspace_id,
                            ConnectorAccountRow.connector == plugin,
                            ConnectorAccountRow.is_active.is_(True),
                        )
                    )
                )
                .scalars()
                .first()
            )
        credentials: dict[str, Any] = {}
        config: dict[str, Any] = {}
        if row is not None:
            credentials = {"token": self._cipher.decrypt(row.signing_secret_ciphertext)}
            config = dict(row.delivery_config or {})
        ctx = SkillContext(llm=_NoLlm(), config=config, logger=logger, credentials=credentials)
        result = await self._runner.dispatch_compensate(
            meta,
            artifact_type=artifact_type,
            context=ctx,
            handle=handle,
        )
        return result if isinstance(result, dict) else {"result": result}


async def get_retract_handler() -> RetractHandler:  # pragma: no cover — overridden in tests
    """Production :class:`RetractHandler` dependency.

    Loads the plugin registry (same path the delivery worker uses) + builds a
    :class:`PluginRetractHandler` over the request-scoped session factory and
    settings-derived :class:`CredentialCipher`. Tests override this with an
    in-test stub so a unit run never touches the loader / KMS.
    """
    from backend.api.deps import _get_session_factory  # noqa: PLC0415 — avoid import cycle
    from backend.extensions.plugin.loader import PluginLoader  # noqa: PLC0415
    from backend.router.accounts.crypto import _key_from_settings  # noqa: PLC0415

    # Lift R1 (v8 §D38) — connector plugins live at repo-root ``plugin/`` —
    # walk up from this module to find it. Path resolution is one-time per
    # request scope and cheap; noqa ASYNC240 mirrors the worker default at
    # ``backend.workflow.infrastructure.workers.run._PLUGINS_IMPLEMENTATIONS_DIR``.
    # NOTE: one extra parent vs the legacy single-file module because this
    # module now lives at ``backend/api/v1/deliverables/_retract_handler.py``
    # (one directory deeper); ``parents[4]`` keeps the repo-root anchor.
    plugin_dir = Path(__file__).resolve().parents[4] / "plugin"  # noqa: ASYNC240
    loader = PluginLoader(plugin_dir)
    registry = await loader.load_all()
    return PluginRetractHandler(
        session_factory=_get_session_factory(),
        plugins_by_name=dict(registry),
        cipher=CredentialCipher(_key_from_settings()),
    )
