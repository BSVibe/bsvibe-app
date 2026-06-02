"""Inbound webhook parser Protocol + ``@webhook(connector)`` decorator (Lift Q3 / R2c).

A plugin that ingests external webhooks (GitHub, Slack, Telegram, Discord,
Sentry, ...) exposes a pure ``(workspace_id, headers, raw_body, secret) ->
TriggerEvent | None`` function. Pre-Lift Q3 the engine reached *into* each
plugin's ``webhook`` module by direct import — a reverse-direction coupling
(plugin → SDK → backend is the canonical direction, never backend → plugin).
The R2c smell that Lift R1 flagged.

This module publishes the canonical surface plugin authors decorate with::

    from bsvibe_sdk import webhook, WebhookSignatureError

    @webhook("github")
    async def parse(workspace_id, headers, raw_body, secret):
        ...
        return TriggerEvent(...)

The engine (``backend.extensions.plugin.loader``) discovers decorated
functions at plugin-load time and registers them with the
``WebhookParserRegistry``; the inbound resolver (``backend.connectors.resolver``)
dispatches by name through that registry instead of importing from
``plugin.<name>.webhook`` directly. The reverse-import goes away.

The Protocol uses ``Any`` for the return value (``TriggerEvent | None``) to
keep the SDK dependency-free of backend types — per v8 §D42 the SDK ships
without backend coupling. Plugin authors annotate concretely; the runtime
duck-types the result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

# Attribute name the engine introspects to discover registered parsers.
# Kept stable across SDK versions — renames break already-loaded plugins.
_WEBHOOK_CONNECTOR_ATTR = "__bsvibe_webhook_connector__"


class WebhookError(ValueError):
    """Raised when a webhook delivery cannot be parsed (malformed input).

    Plugin authors raise this (or a subclass) from their parser. The engine
    treats it as a 400-class outcome at the HTTP boundary.
    """


class WebhookSignatureError(WebhookError):
    """Raised when HMAC / Ed25519 signature verification fails (forgery).

    The HTTP boundary maps this to ``401 Unauthorized``. Plugin authors
    raise it (or a module-local subclass) on every signature mismatch so
    the resolver does not have to know each connector's local error type
    — one ``except WebhookSignatureError:`` catches them all.
    """


@runtime_checkable
class InboundWebhookParser(Protocol):
    """Pure parser: verify signature + map raw delivery to a TriggerEvent.

    Returns the TriggerEvent (concrete type lives in backend.workflow.domain
    — kept ``Any`` here so the SDK stays backend-free) or ``None`` to skip
    the delivery (handshake / unsupported event / bot author). Raises a
    :class:`WebhookSignatureError` subclass on a forged signature.

    The signature must accept ``secret`` even when the connector's
    verifying material is a public key (Discord Ed25519) — passing the
    public key through the same slot keeps the dispatcher uniform.
    """

    async def __call__(
        self,
        *,
        workspace_id: Any,
        headers: dict[str, str],
        raw_body: bytes,
        secret: str | None,
    ) -> Any: ...


def webhook(
    connector: str,
) -> Callable[
    [Callable[..., Awaitable[Any]] | Callable[..., Any]],
    Callable[..., Awaitable[Any]] | Callable[..., Any],
]:
    """Mark a function as the inbound webhook parser for ``connector``.

    The engine's :class:`PluginLoader` discovers decorated parsers via the
    ``__bsvibe_webhook_connector__`` attribute and registers them with the
    in-process :class:`WebhookParserRegistry`. Plugin authors no longer
    need the engine to import ``plugin.<name>.webhook`` directly.

    Both sync and async parsers are accepted — every built-in parser today
    is sync (pure function), but the Protocol declares async to leave
    headroom for future I/O-bound verification (e.g. JWKS fetch).
    """
    if not connector:
        raise ValueError("webhook: connector name must be non-empty")

    def register(
        fn: Callable[..., Awaitable[Any]] | Callable[..., Any],
    ) -> Callable[..., Awaitable[Any]] | Callable[..., Any]:
        setattr(fn, _WEBHOOK_CONNECTOR_ATTR, connector)
        return fn

    return register


__all__ = [
    "InboundWebhookParser",
    "WebhookError",
    "WebhookSignatureError",
    "webhook",
]
