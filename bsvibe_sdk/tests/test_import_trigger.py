"""INV-1 — the ``@p.action(import_trigger=True)`` marker.

Which action imports knowledge must be machine-derivable from ``PluginMeta``
(the single source of truth), not read from a hardcoded backend map. These
tests pin the new ``import_trigger`` field on :class:`ActionCapability` and the
derived :attr:`PluginMeta.import_action_name`.
"""

from __future__ import annotations

import pytest

from bsvibe_sdk import PluginBuilder, PluginRegistrationError, plugin


def _builder() -> PluginBuilder:
    return plugin(name="mock", credentials=[], data_jurisdiction="local")


async def _noop(context: object) -> None:  # pragma: no cover - body unused
    return None


def test_action_import_trigger_defaults_false() -> None:
    """An action declared without ``import_trigger`` is not an import action."""
    p = _builder()
    p.action(name="do_thing")(_noop)
    assert p.meta.actions["do_thing"].import_trigger is False


def test_action_import_trigger_recorded() -> None:
    """``import_trigger=True`` is recorded on the ``ActionCapability``."""
    p = _builder()
    p.action(name="import_stuff", import_trigger=True)(_noop)
    assert p.meta.actions["import_stuff"].import_trigger is True


def test_import_action_name_derives_marked_action() -> None:
    """``PluginMeta.import_action_name`` names the single import-trigger action."""
    p = _builder()
    p.action(name="other")(_noop)
    p.action(name="import_stuff", import_trigger=True)(_noop)
    assert p.meta.import_action_name == "import_stuff"


def test_import_action_name_none_when_unmarked() -> None:
    """No import-trigger action → ``None`` (an outbound-only connector)."""
    p = _builder()
    p.action(name="only_thing")(_noop)
    assert p.meta.import_action_name is None


def test_at_most_one_import_action_asserted() -> None:
    """Declaring two import-trigger actions is a contract violation."""
    p = _builder()
    p.action(name="import_a", import_trigger=True)(_noop)
    p.action(name="import_b", import_trigger=True)(_noop)
    with pytest.raises(PluginRegistrationError):
        _ = p.meta.import_action_name
