"""ConnectorAuthSpec — static auth classification of every connector.

Design §3.1 (2026-06-05). The founder-visible connectors split into three
auth methods, with ``webhook_secret`` an orthogonal axis (any connector that
*receives* inbound webhooks also needs a signing secret for verification):

* ``oauth2``      — github / slack / discord / notion / sentry → "Connect" button
* ``bearer_token``— telegram (bot_token) / email-sender (Resend api_key)
* ``local_path``  — obsidian / claude / gpt → no credential, just a path

This is the auth counterpart to the derived connector catalog
(:func:`backend.connectors.catalog.get_connector_catalog`, which classifies
*capability*: outbound / importable / webhook_trigger). Both MUST cover the
same founder-visible connector set — every user-connectable connector needs an
auth spec. A test asserts ``set(CONNECTOR_AUTH)`` equals the user-connectable
catalog entries.

Backend is the source of truth; the PWA mirrors this into its descriptor
system so the form renders the right control (Connect button / token field /
path field) per connector.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AuthMethod = Literal["oauth2", "bearer_token", "shared_secret", "local_path"]


@dataclass(frozen=True)
class AuthRequirement:
    """The primary credential a connector needs to call its third-party API."""

    method: AuthMethod
    # ``oauth2`` → the registered provider name (== connector name today).
    provider: str | None = None
    # ``bearer_token`` / ``shared_secret`` → the credential field the form
    # renders (e.g. telegram ``bot_token``, email-sender ``api_key``).
    field_key: str | None = None


@dataclass(frozen=True)
class ConnectorAuthSpec:
    """How a connector authenticates, surfaced to the founder UI."""

    connector: str
    primary: AuthRequirement
    # True when the connector receives inbound webhooks and therefore needs a
    # signing secret for signature verification (orthogonal to ``primary``).
    webhook_secret: bool = False
    # Non-secret binding config keys (vault_path, export_path, from, …).
    config_fields: tuple[str, ...] = field(default_factory=tuple)


def _oauth(connector: str, *, webhook_secret: bool) -> ConnectorAuthSpec:
    return ConnectorAuthSpec(
        connector=connector,
        primary=AuthRequirement(method="oauth2", provider=connector),
        webhook_secret=webhook_secret,
    )


CONNECTOR_AUTH: dict[str, ConnectorAuthSpec] = {
    # ── Bucket A: oauth2 (Connect button) ──
    "github": _oauth("github", webhook_secret=True),
    "slack": _oauth("slack", webhook_secret=True),
    "discord": _oauth("discord", webhook_secret=True),
    "notion": _oauth("notion", webhook_secret=False),
    "sentry": _oauth("sentry", webhook_secret=True),
    # ── Bucket B: bearer_token (single credential field) ──
    "telegram": ConnectorAuthSpec(
        connector="telegram",
        primary=AuthRequirement(method="bearer_token", field_key="bot_token"),
        webhook_secret=True,
    ),
    "email-sender": ConnectorAuthSpec(
        connector="email-sender",
        primary=AuthRequirement(method="bearer_token", field_key="api_key"),
        webhook_secret=False,
        config_fields=("from",),
    ),
    # ── Bucket C: local_path (no credential) ──
    "obsidian": ConnectorAuthSpec(
        connector="obsidian",
        primary=AuthRequirement(method="local_path"),
        config_fields=("vault_path", "exclude_patterns", "default_region"),
    ),
    "claude": ConnectorAuthSpec(
        connector="claude",
        primary=AuthRequirement(method="local_path"),
        config_fields=("export_path", "default_region"),
    ),
    "gpt": ConnectorAuthSpec(
        connector="gpt",
        primary=AuthRequirement(method="local_path"),
        config_fields=("export_path", "default_region"),
    ),
}


def auth_spec_for(connector: str) -> ConnectorAuthSpec | None:
    """Return the auth spec for ``connector`` or ``None`` if unknown."""
    return CONNECTOR_AUTH.get(connector)


def oauth_connectors() -> set[str]:
    """Names of connectors whose primary credential is acquired via OAuth."""
    return {name for name, spec in CONNECTOR_AUTH.items() if spec.primary.method == "oauth2"}


__all__ = [
    "CONNECTOR_AUTH",
    "AuthMethod",
    "AuthRequirement",
    "ConnectorAuthSpec",
    "auth_spec_for",
    "oauth_connectors",
]
