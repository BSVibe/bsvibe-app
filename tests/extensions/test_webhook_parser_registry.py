"""Lift Q3 / R2c — engine-side WebhookParserRegistry behaviour.

Verifies the registry honours the SDK ``@webhook(connector)`` contract:

* registers decorated functions by connector name
* normalises Discord's ``public_key=`` kwarg to ``secret=``
* refuses parsers with neither ``secret`` nor ``public_key``
* is_known / get / names round-trip
* :func:`discover_in_module` scans a module's attributes for decorated parsers
* :func:`discover_webhook_parsers` populates the default registry from
  the repo-root ``plugin/`` tree at app startup
"""

from __future__ import annotations

import types
import uuid

import pytest

from backend.extensions.plugin.bootstrap import discover_webhook_parsers
from backend.extensions.plugin.webhook_registry import (
    WebhookParserRegistry,
    discover_in_module,
    reset_default_registry,
)
from bsvibe_sdk import webhook


def test_register_and_get_round_trip() -> None:
    @webhook("github")
    def parse(*, workspace_id, headers, raw_body, secret):  # type: ignore[no-untyped-def]
        return "ok"

    registry = WebhookParserRegistry()
    registry.register("github", parse)

    assert registry.is_known("github") is True
    assert registry.is_known("notion") is False
    assert "github" in registry.names()
    assert registry.get("github") is parse


def test_register_normalizes_public_key_to_secret() -> None:
    """Discord-style parsers (public_key kwarg) get wrapped to take secret."""
    received: dict[str, object] = {}

    @webhook("discord")
    def parse(*, workspace_id, headers, raw_body, public_key):  # type: ignore[no-untyped-def]
        received["public_key"] = public_key
        return "ok"

    registry = WebhookParserRegistry()
    registry.register("discord", parse)
    wrapped = registry.get("discord")
    assert wrapped is not None
    result = wrapped(
        workspace_id=uuid.uuid4(),
        headers={},
        raw_body=b"",
        secret="pub-key-hex",
    )
    assert result == "ok"
    assert received["public_key"] == "pub-key-hex"


def test_register_rejects_parser_without_secret_or_public_key() -> None:
    def parse(*, workspace_id, headers, raw_body):  # type: ignore[no-untyped-def]
        return None

    registry = WebhookParserRegistry()
    with pytest.raises(ValueError, match="secret"):
        registry.register("bogus", parse)


def test_register_empty_connector_is_rejected() -> None:
    def parse(*, workspace_id, headers, raw_body, secret):  # type: ignore[no-untyped-def]
        return None

    registry = WebhookParserRegistry()
    with pytest.raises(ValueError, match="non-empty"):
        registry.register("", parse)


def test_discover_in_module_finds_decorated_callables() -> None:
    @webhook("alpha")
    def alpha_parse(*, workspace_id, headers, raw_body, secret):  # type: ignore[no-untyped-def]
        return None

    def undecorated(*, workspace_id, headers, raw_body, secret):  # type: ignore[no-untyped-def]
        return None

    @webhook("beta")
    async def beta_parse(*, workspace_id, headers, raw_body, secret):  # type: ignore[no-untyped-def]
        return None

    module = types.ModuleType("fake_plugin_webhook")
    module.alpha_parse = alpha_parse  # type: ignore[attr-defined]
    module.undecorated = undecorated  # type: ignore[attr-defined]
    module.beta_parse = beta_parse  # type: ignore[attr-defined]

    found = dict(discover_in_module(module))
    assert found == {"alpha": alpha_parse, "beta": beta_parse}


def test_discover_webhook_parsers_populates_default_registry() -> None:
    """At app bootstrap the 5 built-in connectors land in the default registry."""
    # Reset to verify the call actually populates (vs. relying on prior state).
    reset_default_registry()
    registry = discover_webhook_parsers()

    # All 5 built-in inbound connectors register their parsers.
    expected = {"github", "slack", "telegram", "discord", "sentry"}
    assert expected.issubset(set(registry.names())), (
        f"missing connectors after bootstrap: {expected - set(registry.names())}"
    )


def test_discover_webhook_parsers_is_idempotent() -> None:
    """Repeated startup re-registrations keep the registry stable."""
    reset_default_registry()
    first = discover_webhook_parsers()
    names_first = sorted(first.names())
    second = discover_webhook_parsers()
    assert sorted(second.names()) == names_first
