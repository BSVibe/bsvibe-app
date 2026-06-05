"""Connector AuthStrategy — bsvibe as an OAuth *client* of third parties.

The common skeleton (design ~/Docs/BSVibe_Connector_OAuth_AuthStrategy_Design_
2026-06-05.md) that lets a workspace connect a connector with one click where
the provider supports OAuth, while token / local-path connectors keep their
simpler controls. One :class:`~backend.connectors.auth.providers.OAuthProvider`
interface, three knobs; storage + resolution never branch on provider.
"""

from __future__ import annotations

from backend.connectors.auth.providers import (
    OAuthProvider,
    StubProvider,
    get_provider,
    register_provider,
    registered_providers,
)
from backend.connectors.auth.spec import (
    CONNECTOR_AUTH,
    AuthMethod,
    AuthRequirement,
    ConnectorAuthSpec,
    auth_spec_for,
    oauth_connectors,
)
from backend.connectors.auth.tokenset import TokenSet

__all__ = [
    "CONNECTOR_AUTH",
    "AuthMethod",
    "AuthRequirement",
    "ConnectorAuthSpec",
    "OAuthProvider",
    "StubProvider",
    "TokenSet",
    "auth_spec_for",
    "get_provider",
    "oauth_connectors",
    "register_provider",
    "registered_providers",
]
