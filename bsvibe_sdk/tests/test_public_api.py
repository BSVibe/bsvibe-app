"""Lift S — bsvibe_sdk public surface contract.

Verifies the SDK exposes exactly the Plugin-author-facing types declared in
``~/Docs/BSVibe_Class_Architecture_Design_2026-05-30.md`` v8 §13 Lift S +
D39 + D42, and no more.

The SDK is plugin-only (D42): no Skill Protocol, no engine internals.
"""

from __future__ import annotations

from typing import Protocol, get_type_hints

import pytest


def test_top_level_imports() -> None:
    """The public surface is importable via the top-level package."""
    from bsvibe_sdk import (  # noqa: F401
        Action,
        Context,
        EventBusSubscriber,
        Plugin,
        Result,
        __version__,
        action,
        on_event,
        plugin,
    )


def test_version_is_pep440_string() -> None:
    from bsvibe_sdk import __version__

    assert isinstance(__version__, str)
    # Minimum: looks like 0.1.0 / 0.1.0a1 / etc.
    assert __version__[0].isdigit()
    assert "." in __version__


@pytest.mark.parametrize("name", ["Plugin", "Action", "EventBusSubscriber"])
def test_protocols_are_runtime_checkable(name: str) -> None:
    import bsvibe_sdk

    proto = getattr(bsvibe_sdk, name)
    assert issubclass(proto, Protocol), f"{name} must be a Protocol subclass"
    assert getattr(proto, "_is_runtime_protocol", False), (
        f"{name} must be marked @runtime_checkable"
    )


def test_no_skill_in_public_surface() -> None:
    """D42 — SDK is plugin-only. Skills are yaml+md data, not an SDK contract."""
    import bsvibe_sdk

    assert not hasattr(bsvibe_sdk, "Skill"), (
        "Lift S (D42) — SDK must not export Skill; skills are data, not code"
    )
    assert not hasattr(bsvibe_sdk, "SkillDecorator")
    assert "Skill" not in bsvibe_sdk.__all__


def test_no_heavy_deps_imported() -> None:
    """The SDK must not transitively import FastAPI / SQLAlchemy / LiteLLM."""
    import sys

    # Drop any cached modules so we observe a fresh import.
    for mod in list(sys.modules):
        if mod.startswith("bsvibe_sdk"):
            del sys.modules[mod]

    import bsvibe_sdk  # noqa: F401

    forbidden = {"fastapi", "sqlalchemy", "sqlmodel", "litellm", "structlog"}
    leaked = forbidden & set(sys.modules)
    assert leaked == set(), (
        f"bsvibe_sdk pulled in heavy deps: {sorted(leaked)}. "
        "SDK must be plugin-author-facing only with zero heavy imports."
    )


def test_plugin_protocol_minimal_conformance() -> None:
    """A mock object exposing the minimal Plugin shape must isinstance-conform."""
    from bsvibe_sdk import Plugin

    class _MockPlugin:
        name = "mock"

        def list_actions(self) -> list[str]:
            return ["noop"]

    assert isinstance(_MockPlugin(), Plugin)


def test_action_protocol_minimal_conformance() -> None:
    from bsvibe_sdk import Action

    class _MockAction:
        name = "noop"

        async def __call__(self, context: object, /, **kwargs: object) -> None:
            return None

    assert isinstance(_MockAction(), Action)


def test_event_bus_subscriber_protocol_minimal_conformance() -> None:
    from bsvibe_sdk import Event, EventBusSubscriber

    class _MockSubscriber:
        async def on_event(self, event: Event) -> None:
            return None

    assert isinstance(_MockSubscriber(), EventBusSubscriber)


def test_plugin_decorator_is_callable_factory() -> None:
    """``plugin(name=..., ...)`` returns a builder with the capability decorators."""
    from bsvibe_sdk import plugin

    builder = plugin(
        name="mock",
        credentials=[],
        data_jurisdiction="local",
    )
    # The builder must expose the four primary capability decorators.
    assert callable(builder.action)
    assert callable(builder.inbound)
    assert callable(builder.outbound)
    assert callable(builder.setup)


def test_action_decorator_returns_callable() -> None:
    """``@action(name=...)`` is the standalone decorator alias usable on free fns."""
    from bsvibe_sdk import action

    deco = action(name="noop")
    assert callable(deco)


def test_on_event_decorator_returns_callable() -> None:
    """``@on_event(kind_prefix=...)`` marks a function as an event subscriber."""
    from bsvibe_sdk import on_event

    deco = on_event(kind_prefix="audit.")
    assert callable(deco)


def test_context_dataclass_has_required_fields() -> None:
    from bsvibe_sdk import Context

    hints = get_type_hints(Context)
    # The plugin runtime injects logger + config + credentials + input_data
    # at minimum (mirrors backend SkillContext).
    for field in ("logger", "config", "credentials"):
        assert field in hints, f"Context missing required field {field!r}"


def test_result_helper_constructs() -> None:
    from bsvibe_sdk import Result

    ok = Result.ok({"foo": 1})
    assert ok.success is True
    assert ok.data == {"foo": 1}

    err = Result.err("boom")
    assert err.success is False
    assert err.error == "boom"


def test_all_export_is_complete() -> None:
    """``__all__`` must exactly cover the documented public surface."""
    import bsvibe_sdk

    expected = {
        "Plugin",
        "plugin",
        "Action",
        "action",
        "EventBusSubscriber",
        "on_event",
        "Event",
        "Context",
        "Result",
        "__version__",
    }
    assert set(bsvibe_sdk.__all__) == expected, (
        f"__all__ drift: extra={set(bsvibe_sdk.__all__) - expected}, "
        f"missing={expected - set(bsvibe_sdk.__all__)}"
    )


def test_py_typed_marker_present() -> None:
    """PEP 561 marker — without it, mypy treats the package as untyped."""
    from pathlib import Path

    import bsvibe_sdk

    pkg_dir = Path(bsvibe_sdk.__file__).parent
    assert (pkg_dir / "py.typed").exists(), "PEP 561 py.typed marker missing"
