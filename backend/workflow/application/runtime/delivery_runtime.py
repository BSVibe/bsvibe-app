"""Delivery dispatch + audit-relay runtime (§17.2a slice).

Four pieces, all centered on the *Delivery* side of the worker runtime:

* :data:`_PLUGINS_IMPLEMENTATIONS_DIR` — repo-rooted plugins/ default
  (Lift R1 / v8 §D38). Resolution walks up from
  ``runtime/delivery_runtime.py`` to the repo root and points at
  ``<repo_root>/plugin``.
* :class:`RealPluginDispatchAdapter` — bridges the worker's
  ``PluginDispatchAdapter`` Protocol to the real
  :class:`DeliveryDispatcher` over the loaded plugin set.
* :func:`build_delivery_adapter` — loads every plugin under
  ``plugins_dir`` + wraps a connector-bound adapter.
* :func:`load_connector_plugins` — loads the plugin registry once at boot
  so the native agent loop can surface a workspace's connector actions
  as tools (B5b).
* :class:`LoggingRelay` — drains the audit outbox (no remote connector
  in this slice — the real audit relay is config-driven via
  :func:`build_relay`).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings, get_settings
from backend.extensions.plugin.base import PluginMeta
from backend.extensions.plugin.loader import PluginLoader
from backend.extensions.plugin.runner import PluginRunner
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.workflow.application.delivery.connector_dispatch import (
    ConnectorDeliveryAdapter,
    build_connector_delivery_adapter,
)
from backend.workflow.application.delivery.dispatcher import DeliveryDispatcher
from backend.workflow.domain.delivery import DeliveryResult

logger = structlog.get_logger(__name__)


# Default plugin-implementations directory (scanned at module import, in sync
# context, so the async loader path stays free of filesystem-resolve calls).
#
# Lift R1 (v8 §D38): connector plugins live at repo-root ``plugin/<name>/``,
# not under ``backend/extensions/implementations/`` (which now holds only the
# yet-to-be-relocated audit plugin pending Lift R2's EventBus rewire).
# Resolution: from ``backend/workflow/application/runtime/delivery_runtime.py``
# walk up to the repo root (5 parents: runtime → application → workflow →
# backend → repo root) and point at ``<repo_root>/plugin``.
# ``settings.plugins_dir`` overrides for tests / non-standard deploy layouts.
_PLUGINS_IMPLEMENTATIONS_DIR = Path(__file__).resolve().parents[4] / "plugin"


class RealPluginDispatchAdapter:
    """Bridges the worker's ``PluginDispatchAdapter`` Protocol to the real
    :class:`DeliveryDispatcher` over the loaded plugins.

    The worker calls ``dispatch(workspace_id, deliverable_id, artifact_type)``;
    this adapter supplies the loaded plugin list. The dispatcher itself filters
    by ``artifact_type`` (a plugin without a matching outbound skips silently),
    so a workspace with no matching plugin yields an empty (but successful)
    :class:`DeliveryResult` — the event still drains, never wedging the queue.
    """

    def __init__(
        self,
        *,
        plugins: list[PluginMeta],
        dispatcher: DeliveryDispatcher | None = None,
    ) -> None:
        self._plugins = plugins
        self._dispatcher = dispatcher or DeliveryDispatcher(runner=PluginRunner())

    async def dispatch(
        self,
        *,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
        artifact_type: str,
        plugins: Any = (),
        context: Any = None,
        event: Any = None,
    ) -> DeliveryResult:
        return await self._dispatcher.dispatch(
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,  # type: ignore[arg-type]  # validated downstream
            plugins=self._plugins,
            context=context,
            event=event,
        )


async def build_delivery_adapter(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    plugins_dir: Path | None = None,
) -> ConnectorDeliveryAdapter:
    """Load every plugin under ``plugins_dir`` + wrap a connector-bound adapter.

    The production delivery path resolves the run workspace's configured
    ``connector_accounts`` (binding = ``delivery_config``), shapes the connector
    outbound event from the Deliverable content + that stable config, and
    dispatches THAT connector's ``@p.outbound`` — closing the verified-Deliverable
    → external-delivery loop. v1 ships the notion event mapper; other connectors
    are a registered seam (see :mod:`backend.workflow.application.delivery.connector_dispatch`).

    The adapter decrypts the per-account outbound credential
    (``signing_secret_ciphertext``) with the settings-derived
    :class:`CredentialCipher` and opens its own session per dispatch (it must
    load the Deliverable + resolve the binding), so it carries a session
    factory rather than borrowing the worker's row-scoped session.
    """
    root = plugins_dir or _PLUGINS_IMPLEMENTATIONS_DIR
    loader = PluginLoader(root)
    registry = await loader.load_all()
    logger.info("worker_runtime_plugins_loaded", count=len(registry), names=sorted(registry))
    # workspace_root lets the github special case find the run's checkout
    # (``run_workspace_root/<run_id>``) to commit + push before opening the PR.
    return build_connector_delivery_adapter(
        session_factory=session_factory,
        plugins=list(registry.values()),
        cipher=CredentialCipher(_key_from_settings()),
        workspace_root=Path(get_settings().run_workspace_root),
    )


async def load_connector_plugins(
    *,
    settings: Settings | None = None,
    plugins_dir: Path | None = None,
) -> dict[str, PluginMeta]:
    """Load the plugin registry (B5b).

    Returns ``plugins_by_name`` so the native agent loop can surface the
    workspace's ``mcp_exposed`` connector actions as tools. Lift 0c removed
    the load-time danger scanner — the returned registry no longer carries a
    verdict map."""
    settings = settings or get_settings()
    root = plugins_dir or _PLUGINS_IMPLEMENTATIONS_DIR
    loader = PluginLoader(root)
    registry = await loader.load_all()
    logger.info(
        "connector_action_plugins_loaded",
        count=len(registry),
    )
    return dict(registry)


class LoggingRelay:
    """A :class:`~backend.workflow.infrastructure.workers.relay_worker.Relay`
    that acknowledges every record after logging it.

    The remote audit sink (HTTP/gRPC connector) is out of scope for the
    worker-runtime chunk; this relay drains the outbox so audit rows do not
    accumulate unboundedly, by acking the whole batch (every id delivered).
    """

    async def send(self, records: Any) -> list[int]:
        ids = [r.id for r in records]
        if ids:
            logger.info("worker_runtime_relay_acked", count=len(ids))
        return ids


__all__ = [
    "LoggingRelay",
    "RealPluginDispatchAdapter",
    "_PLUGINS_IMPLEMENTATIONS_DIR",
    "build_delivery_adapter",
    "load_connector_plugins",
]
