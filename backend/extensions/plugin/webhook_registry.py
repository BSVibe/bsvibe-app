"""Engine-side registry of inbound webhook parsers (Lift Q3 / R2c).

Plugin authors decorate their parser function with
:func:`bsvibe_sdk.webhook` and the :class:`backend.extensions.plugin.PluginLoader`
discovers them at startup, calling :meth:`WebhookParserRegistry.register` for
each marked function. The connector inbound resolver
(:class:`backend.connectors.resolver.ConnectorInboundResolver`) then looks
parsers up by connector name through this registry instead of importing
``plugin.<name>.webhook`` directly — the reverse-direction coupling that
Lift R1 flagged (R2c) goes away.

Design notes
------------
* The registry is a thin mapping (``dict[str, ConnectorParser]``); the
  loader populates it and the resolver consumes a frozen snapshot. We do
  NOT publish a process-wide singleton — every caller receives an
  explicit :class:`WebhookParserRegistry` instance via DI, so pytest can
  build a fresh registry per test without process-wide state leaks
  (v8 §22 pattern #6 — no module-level mutable state).
* Parsers are exposed as plain ``Callable`` (matching the
  ``(workspace_id, headers, raw_body, secret) -> TriggerEvent | None``
  shape every existing built-in already follows). The
  :class:`bsvibe_sdk.InboundWebhookParser` Protocol describes the same
  shape but uses ``Any`` for the return so the SDK stays backend-free —
  this module narrows it to the concrete ``TriggerEvent``.
* Discord's parser names its keyword ``public_key`` instead of ``secret``
  (Ed25519 verifying material). The engine wraps it at registration time
  so every parser in the registry obeys the uniform ``secret=`` keyword
  — the resolver does not need to know each connector's local kwarg
  convention.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from typing import Any

import structlog
from bsvibe_sdk.webhook import _WEBHOOK_CONNECTOR_ATTR

from backend.workflow.domain.incoming import TriggerEvent

logger = structlog.get_logger(__name__)


# A connector parser: verify the signature with ``secret`` and map the raw
# delivery to a :class:`TriggerEvent` (or ``None`` to skip).
ConnectorParser = Callable[..., TriggerEvent | None]


class WebhookParserRegistry:
    """Mapping of connector name → parser, populated by the plugin loader."""

    def __init__(self) -> None:
        self._parsers: dict[str, ConnectorParser] = {}

    def clear(self) -> None:
        """Drop every registered parser. Test-only helper."""
        self._parsers.clear()

    def register(self, connector: str, parser: Callable[..., Any]) -> None:
        """Register ``parser`` for ``connector`` (idempotent re-register wins).

        Wraps Discord-style parsers that name their verifying material
        ``public_key`` into the uniform ``secret`` keyword the resolver
        dispatches through.
        """
        if not connector:
            raise ValueError("WebhookParserRegistry: connector must be non-empty")

        wrapped = _normalize_parser(parser)
        previous = self._parsers.get(connector)
        self._parsers[connector] = wrapped
        if previous is None:
            logger.info("webhook_parser_registered", connector=connector)
        else:
            logger.info("webhook_parser_replaced", connector=connector)

    def get(self, connector: str) -> ConnectorParser | None:
        return self._parsers.get(connector)

    def is_known(self, connector: str) -> bool:
        return connector in self._parsers

    def names(self) -> list[str]:
        return sorted(self._parsers)

    def as_mapping(self) -> dict[str, ConnectorParser]:
        """Return a shallow copy of the (connector → parser) map."""
        return dict(self._parsers)


def _normalize_parser(parser: Callable[..., Any]) -> ConnectorParser:
    """Adapt a parser to the uniform ``secret=`` keyword.

    Discord's plugin parser uses ``public_key`` (Ed25519 verifying
    material); we accept it as the same semantic slot so the resolver can
    dispatch every connector through one call shape.
    """
    sig = inspect.signature(parser)
    params = sig.parameters
    if "secret" in params:
        return parser
    if "public_key" in params:

        def adapter(
            *,
            workspace_id: uuid.UUID,
            headers: dict[str, str],
            raw_body: bytes,
            secret: str | None,
        ) -> TriggerEvent | None:
            return parser(  # type: ignore[no-any-return]
                workspace_id=workspace_id,
                headers=headers,
                raw_body=raw_body,
                public_key=secret,
            )

        return adapter
    raise ValueError(f"webhook parser {parser!r} must accept 'secret' (or 'public_key') kwarg")


def discover_in_module(module: Any) -> list[tuple[str, Callable[..., Any]]]:
    """Return ``(connector_name, fn)`` pairs for every parser in ``module``.

    Scans the module's attributes for callables marked with the
    :func:`bsvibe_sdk.webhook` decorator (those carrying the
    ``__bsvibe_webhook_connector__`` attribute). The plugin loader calls
    this on every plugin module after importing it.
    """
    out: list[tuple[str, Callable[..., Any]]] = []
    for attr_name in dir(module):
        obj = getattr(module, attr_name, None)
        if obj is None or not callable(obj):
            continue
        connector = getattr(obj, _WEBHOOK_CONNECTOR_ATTR, None)
        if isinstance(connector, str) and connector:
            out.append((connector, obj))
    return out


# --------------------------------------------------------------------------- #
# Process-wide default registry — populated by the app's plugin-loader        #
# bootstrap. The HTTP-boundary route + the founder-facing CRUD validator      #
# share this singleton so they agree on the set of known connectors without   #
# carrying an explicit registry argument through every dependency.            #
# Tests construct their own :class:`WebhookParserRegistry` and bypass this    #
# singleton via DI.                                                           #
# --------------------------------------------------------------------------- #


_DEFAULT_REGISTRY = WebhookParserRegistry()


def get_default_registry() -> WebhookParserRegistry:
    """Return the process-wide default :class:`WebhookParserRegistry`."""
    return _DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """Drop every parser from the default registry. Test-only helper.

    Mutates the existing singleton in place rather than rebinding the
    module-level reference so callers that already hold a reference (e.g.
    a FastAPI ``dependency_overrides`` capture in a long-lived test app)
    observe the reset.
    """
    _DEFAULT_REGISTRY.clear()


__all__ = [
    "ConnectorParser",
    "WebhookParserRegistry",
    "discover_in_module",
    "get_default_registry",
    "reset_default_registry",
]
