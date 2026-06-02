"""Startup-time helpers for the plugin extension surface.

The FastAPI app process needs the engine's :class:`WebhookParserRegistry`
populated before the first request — otherwise the public webhook
ingress (``/api/webhooks/{connector}/{token}``) would 404 every delivery.
The worker process gets this for free via :func:`load_connector_plugins`
(which already runs :meth:`PluginLoader.load_all`), but the API process
historically depended on module-import side effects in
``backend.connectors.resolver``. After Lift Q3 / R2c that direct
``from plugin.<name>.webhook import …`` is gone, so the API factory
explicitly populates the registry at app construction time via
:func:`discover_webhook_parsers`.

The function is intentionally synchronous (the API ``create_app`` is
sync) and idempotent — re-registering the same connector overwrites the
existing entry with the same callable, a no-op.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import structlog

from backend.extensions.plugin.webhook_registry import (
    WebhookParserRegistry,
    discover_in_module,
    get_default_registry,
)

logger = structlog.get_logger(__name__)


# Repo-root plugin/ tree (Lift R1). Resolved relative to this file:
#   backend/extensions/plugin/bootstrap.py → repo-root → plugin/
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_PLUGINS_DIR = _REPO_ROOT / "plugin"


def discover_webhook_parsers(
    *,
    plugins_dir: Path | None = None,
    registry: WebhookParserRegistry | None = None,
) -> WebhookParserRegistry:
    """Import every ``plugin/<name>/webhook.py`` and register decorated parsers.

    The default target is the process-wide :func:`get_default_registry`
    singleton; tests pass an explicit :class:`WebhookParserRegistry` to
    keep registrations local.

    Soft-fails per plugin — a missing ``webhook.py`` or an import-time
    error is logged at warning level and skipped. The plugin's other
    capabilities (action / outbound / compensate) come from the separate
    :meth:`PluginLoader.load_all` path and are not affected here.
    """
    target = registry if registry is not None else get_default_registry()
    root = plugins_dir or _DEFAULT_PLUGINS_DIR
    if not root.is_dir():
        logger.warning("plugins_dir_missing", path=str(root))
        return target

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(".") or name.startswith("__"):
            continue
        webhook_py = entry / "webhook.py"
        if not webhook_py.exists():
            continue
        module_name = f"plugin.{name}.webhook"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin_webhook_import_failed", module=module_name, error=str(exc))
            continue
        for connector, fn in discover_in_module(module):
            target.register(connector, fn)
    logger.info("webhook_parsers_discovered", count=len(target.names()), names=target.names())
    return target


__all__ = ["discover_webhook_parsers"]
