"""Lift G — smoke import surface for the new Extensions context.

Asserts:
* new paths resolve (engine merge + audit move + Protocol stubs).
* stale paths (``backend.plugins`` / ``backend.skills`` /
  ``backend.supervisor.audit``) raise ``ModuleNotFoundError``.
"""

from __future__ import annotations

import importlib

import pytest

# Stale paths kept as string parts so the global rename sweep can't
# silently rewrite them. The test enforces that the *old* dotted paths
# stay non-importable after Lift G.
_PLUGINS = "backend." + "plugins"
_SKILLS = "backend." + "skills"
_SUP_AUDIT = "backend." + "supervisor.audit"


def test_new_engine_paths_importable() -> None:
    plugin_loader_mod = importlib.import_module("backend.extensions.plugin.loader")
    assert hasattr(plugin_loader_mod, "PluginLoader")

    plugin_runner_mod = importlib.import_module("backend.extensions.plugin.runner")
    assert hasattr(plugin_runner_mod, "PluginRunner")

    skill_loader_mod = importlib.import_module("backend.extensions.skill.loader")
    assert hasattr(skill_loader_mod, "SkillLoader")

    skill_runner_mod = importlib.import_module("backend.extensions.skill.runner")
    assert hasattr(skill_runner_mod, "invoke_skill")


def test_extension_protocols_importable() -> None:
    protocols = importlib.import_module("backend.extensions.domain.protocols")
    for name in (
        "ActionDispatchInterceptor",
        "SettlementSubscriber",
        "EventBus",
        "EventBusSubscriber",
        "Plugin",
        "Skill",
        "Action",
    ):
        assert hasattr(protocols, name), f"missing protocol: {name}"


def test_audit_relocated_under_extensions() -> None:
    audit_mod = importlib.import_module("backend.extensions.implementations.audit")
    for name in (
        "AuditEmitter",
        "AuditEvent",
        "AuditActor",
        "AuditResource",
        "safe_emit",
        "make_actor",
        "OutboxStore",
    ):
        assert hasattr(audit_mod, name), f"missing audit re-export: {name}"


def test_extensions_top_level_union_reexport() -> None:
    ext = importlib.import_module("backend.extensions")
    for name in (
        "PluginLoader",
        "PluginRunner",
        "SkillLoader",
        "invoke_skill",
        "PluginBuilder",
    ):
        assert hasattr(ext, name), f"missing top-level re-export: {name}"


@pytest.mark.parametrize(
    "stale",
    [
        _PLUGINS,
        f"{_PLUGINS}.loader",
        f"{_PLUGINS}.runner",
        f"{_PLUGINS}.base",
        f"{_PLUGINS}.context",
        f"{_PLUGINS}.decorator",
        _SKILLS,
        f"{_SKILLS}.loader",
        f"{_SKILLS}.runner",
        f"{_SKILLS}.meta",
        f"{_SKILLS}.tool_binding",
        f"{_SKILLS}.exceptions",
        _SUP_AUDIT,
        f"{_SUP_AUDIT}.emitter",
        f"{_SUP_AUDIT}.events",
        f"{_SUP_AUDIT}.models",
        f"{_SUP_AUDIT}.service",
        f"{_SUP_AUDIT}.store",
    ],
)
def test_stale_import_paths_gone(stale: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(stale)
