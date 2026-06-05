"""OAuthProvider — the one auth interface every third-party connector uses.

Design §3 (2026-06-05 classification): github / slack / discord / notion /
sentry all share the ``authorization_code`` grant. They do NOT need separate
auth *methods*; the only per-provider variation is three knobs:

* ``token_exchange_auth`` — how the token endpoint authenticates the client:
  ``body`` (GitHub, Slack), ``basic`` (Discord, Notion), ``jwt`` (GitHub App
  installation, Sentry refresh).
* ``refreshable`` — whether the provider issues refresh material.
* ``supports_service_token`` — whether the provider can mint a token to act
  WITHOUT a user present (GitHub App installation token, Sentry JWT). This is
  the genuinely-special capability that made ``github_app`` look like its own
  method — but Sentry shares it, so it belongs here as an optional capability,
  not a top-level method.

``StubProvider`` is the test double the Lift 0 skeleton is built against — no
real provider ships until Lift 1 (GitHubAppProvider).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from backend.connectors.auth.tokenset import TokenSet

TokenExchangeAuth = Literal["body", "basic", "jwt"]


@runtime_checkable
class OAuthProvider(Protocol):
    """Acquire + refresh credentials for one third-party connector.

    A provider is bsvibe acting as an OAuth *client* of the third party —
    the opposite direction from :mod:`backend.identity.oauth_service`, where
    bsvibe is the authorization *server*.
    """

    name: str
    token_exchange_auth: TokenExchangeAuth
    refreshable: bool
    supports_service_token: bool

    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        """Build the provider's authorize-redirect URL (response_type=code)."""
        ...

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        """Exchange an authorization code for a token set."""
        ...

    async def refresh(self, *, refresh_token: str) -> TokenSet:
        """Mint a fresh token set from refresh material (or a re-signed JWT)."""
        ...

    async def service_token(self, *, install_ref: str) -> TokenSet:
        """Mint a token to act without a user (installation / JWT flow).

        Providers with ``supports_service_token=False`` raise
        :class:`NotImplementedError`.
        """
        ...


@dataclass
class StubProvider:
    """In-memory provider used by the Lift 0 skeleton + tests.

    Returns deterministic canned tokens so the storage / endpoint / resolution
    layers can be exercised end-to-end before any real provider exists.
    """

    name: str = "stub"
    token_exchange_auth: TokenExchangeAuth = "body"  # noqa: S105 — auth-style label, not a secret
    refreshable: bool = True
    supports_service_token: bool = False
    authorize_base: str = "https://stub.invalid/authorize"

    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        return (
            f"{self.authorize_base}"
            f"?state={state}"
            f"&code_challenge={code_challenge}"
            f"&redirect_uri={redirect_uri}"
        )

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        return TokenSet(
            access_token=f"stub-access-{code}",
            refresh_token="stub-refresh" if self.refreshable else None,
            expires_at=None,
            scopes=(),
            account_label="stub-user",
        )

    async def refresh(self, *, refresh_token: str) -> TokenSet:
        return TokenSet(
            access_token="stub-access-refreshed",  # noqa: S106 — canned stub token, not a secret
            refresh_token=refresh_token,
            expires_at=None,
            scopes=(),
            account_label="stub-user",
        )

    async def service_token(self, *, install_ref: str) -> TokenSet:
        if not self.supports_service_token:
            raise NotImplementedError(f"provider {self.name!r} does not support service tokens")
        return TokenSet(
            access_token=f"stub-service-{install_ref}",
            refresh_token=None,
            expires_at=None,
            scopes=(),
            account_label="stub-install",
        )


# Process-wide provider registry. Lift 0 seeds only the stub; real providers
# (GitHubAppProvider, …) register themselves from Lift 1 onward.
_REGISTRY: dict[str, OAuthProvider] = {}


def register_provider(provider: OAuthProvider) -> None:
    """Register ``provider`` under its ``name`` (overwrites an existing one)."""
    _REGISTRY[provider.name] = provider


def get_provider(name: str) -> OAuthProvider | None:
    """Return the registered provider for ``name`` or ``None`` if unknown."""
    return _REGISTRY.get(name)


def registered_providers() -> tuple[str, ...]:
    """Names of all currently registered providers (sorted, deterministic)."""
    return tuple(sorted(_REGISTRY))


# Seed the stub so the skeleton is exercisable out of the box.
register_provider(StubProvider())


__all__ = [
    "OAuthProvider",
    "StubProvider",
    "TokenExchangeAuth",
    "get_provider",
    "register_provider",
    "registered_providers",
]
