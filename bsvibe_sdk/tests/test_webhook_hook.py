"""Lift Q3 / R2c — bsvibe_sdk inbound-webhook hook.

Verifies the new ``@webhook(connector)`` decorator + Protocol +
SDK-level base error classes are exposed from the public surface and
behave as the engine's discovery contract expects.

Coupled with ``test_plugin_webhook_registration`` (engine side) and
``test_r2c_no_reverse_imports`` (architectural cleanup gate).
"""

from __future__ import annotations

from typing import Protocol

import pytest


def test_webhook_decorator_marks_function_with_connector_attr() -> None:
    """The decorator sets a stable attribute the engine can introspect."""
    from bsvibe_sdk import webhook

    @webhook("github")
    def parse(*, workspace_id, headers, raw_body, secret):  # type: ignore[no-untyped-def]
        return None

    assert getattr(parse, "__bsvibe_webhook_connector__", None) == "github"


def test_webhook_decorator_preserves_function_identity() -> None:
    """``@webhook`` is a marker — the returned function is the same object."""
    from bsvibe_sdk import webhook

    def parse(*, workspace_id, headers, raw_body, secret):  # type: ignore[no-untyped-def]
        return None

    decorated = webhook("slack")(parse)
    assert decorated is parse


def test_webhook_decorator_rejects_empty_connector() -> None:
    from bsvibe_sdk import webhook

    with pytest.raises(ValueError, match="non-empty"):
        webhook("")


def test_inbound_webhook_parser_protocol_is_runtime_checkable() -> None:
    """A minimally-conforming async parser must isinstance-check as the Protocol."""
    from bsvibe_sdk import InboundWebhookParser

    assert issubclass(InboundWebhookParser, Protocol)
    assert getattr(InboundWebhookParser, "_is_runtime_protocol", False)


def test_webhook_signature_error_is_subclass_of_webhook_error() -> None:
    """Plugin code can catch a single SDK base — every connector's local
    ``WebhookSignatureError`` subclasses the SDK base, so the engine's
    one ``except WebhookSignatureError`` covers them all."""
    from bsvibe_sdk import WebhookError, WebhookSignatureError

    assert issubclass(WebhookSignatureError, WebhookError)
    assert issubclass(WebhookError, ValueError)


def test_webhook_decorator_works_on_async_functions() -> None:
    from bsvibe_sdk import webhook

    @webhook("telegram")
    async def parse(*, workspace_id, headers, raw_body, secret):  # type: ignore[no-untyped-def]
        return None

    assert getattr(parse, "__bsvibe_webhook_connector__", None) == "telegram"


def test_webhook_decorator_records_distinct_connectors() -> None:
    from bsvibe_sdk import webhook

    @webhook("a")
    def parse_a(*, workspace_id, headers, raw_body, secret):  # type: ignore[no-untyped-def]
        return None

    @webhook("b")
    def parse_b(*, workspace_id, headers, raw_body, secret):  # type: ignore[no-untyped-def]
        return None

    assert parse_a.__bsvibe_webhook_connector__ == "a"
    assert parse_b.__bsvibe_webhook_connector__ == "b"
