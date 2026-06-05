"""register_configured_providers — settings → live provider registry (Lift 1).

The Lift 0 skeleton seeds only the StubProvider at import time. Real providers
are credential-gated: GitHubAppProvider registers under ``github`` ONLY when
its App credentials are configured, so a deployment without GitHub creds simply
has no github provider (the connector falls back to the legacy secret path).

A snapshot/restore fixture keeps each test from leaking into the process-wide
``_REGISTRY``.
"""

from __future__ import annotations

import pytest

from backend.config import Settings
from backend.connectors.auth import providers as providers_mod
from backend.connectors.auth.bootstrap import register_configured_providers
from backend.connectors.auth.github import GitHubAppProvider
from backend.connectors.auth.providers import get_provider


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    snapshot = dict(providers_mod._REGISTRY)
    try:
        yield
    finally:
        providers_mod._REGISTRY.clear()
        providers_mod._REGISTRY.update(snapshot)


def test_full_app_creds_register_github_with_service_token() -> None:
    settings = Settings(
        github_app_client_id="Iv1.cid",
        github_app_client_secret="csecret",
        github_app_id="123456",
        github_app_private_key_pem="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
    )
    registered = register_configured_providers(settings)
    assert "github" in registered
    prov = get_provider("github")
    assert isinstance(prov, GitHubAppProvider)
    assert prov.supports_service_token is True


def test_client_only_creds_register_github_without_service_token() -> None:
    settings = Settings(
        github_app_client_id="Iv1.cid",
        github_app_client_secret="csecret",
    )
    registered = register_configured_providers(settings)
    assert registered == ["github"]
    prov = get_provider("github")
    assert isinstance(prov, GitHubAppProvider)
    # No app_id / private key → installation token capability stays off.
    assert prov.supports_service_token is False


def test_no_creds_register_nothing() -> None:
    settings = Settings(github_app_client_id="", github_app_client_secret="")
    registered = register_configured_providers(settings)
    assert registered == []
    assert get_provider("github") is None


def test_slack_registered_from_env() -> None:
    settings = Settings(slack_client_id="cid", slack_client_secret="sec")
    registered = register_configured_providers(settings)
    assert "slack" in registered
    prov = get_provider("slack")
    assert prov is not None
    assert prov.name == "slack"


def test_vanilla_provider_not_registered_without_both_creds() -> None:
    settings = Settings(slack_client_id="cid", slack_client_secret="")
    registered = register_configured_providers(settings)
    assert "slack" not in registered
