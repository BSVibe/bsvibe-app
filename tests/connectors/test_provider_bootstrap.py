"""register_configured_providers — settings → live provider registry.

Real providers are credential-gated. github is the exception: its App creds
live in the DB (set up via the manifest flow), NOT env — so github is NEVER
registered from env here; it is loaded by ``load_app_credential_providers`` at
startup. slack / notion / discord stay env-registered (until their own DB
setup lift).

A snapshot/restore fixture keeps each test from leaking into the process-wide
``_REGISTRY``.
"""

from __future__ import annotations

import pytest

from backend.config import Settings
from backend.connectors.auth import providers as providers_mod
from backend.connectors.auth.bootstrap import register_configured_providers
from backend.connectors.auth.providers import get_provider


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    snapshot = dict(providers_mod._REGISTRY)
    try:
        yield
    finally:
        providers_mod._REGISTRY.clear()
        providers_mod._REGISTRY.update(snapshot)


def test_github_not_registered_from_env(monkeypatch) -> None:
    # Even with the legacy BSVIBE_GITHUB_APP_* env vars set, github is NOT
    # registered from env — its creds come from the DB (manifest flow) only.
    monkeypatch.setenv("BSVIBE_GITHUB_APP_CLIENT_ID", "Iv1.cid")
    monkeypatch.setenv("BSVIBE_GITHUB_APP_CLIENT_SECRET", "csecret")
    registered = register_configured_providers(Settings())
    assert "github" not in registered
    assert get_provider("github") is None


def test_no_creds_register_nothing(monkeypatch) -> None:
    for var in (
        "BSVIBE_SLACK_CLIENT_ID",
        "BSVIBE_SLACK_CLIENT_SECRET",
        "BSVIBE_NOTION_CLIENT_ID",
        "BSVIBE_NOTION_CLIENT_SECRET",
        "BSVIBE_DISCORD_CLIENT_ID",
        "BSVIBE_DISCORD_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    registered = register_configured_providers(Settings())
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


def test_notion_and_discord_registered_from_env() -> None:
    settings = Settings(
        notion_client_id="n",
        notion_client_secret="ns",
        discord_client_id="d",
        discord_client_secret="ds",
    )
    registered = register_configured_providers(settings)
    assert "notion" in registered
    assert "discord" in registered
    assert get_provider("notion") is not None
    assert get_provider("discord") is not None
