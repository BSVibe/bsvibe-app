"""Core dataclasses + errors for the BSVibe plugin framework.

Workflow §6 #4 capability model — a single :class:`PluginMeta` carries
zero or more *capabilities* (inbound / outbound / action) plus an
optional one-shot setup function. The legacy ``category`` field and the
same-channel ``@execute.notify`` assumption are dropped here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

VALID_TRIGGER_TYPES = frozenset(
    {"cron", "webhook", "on_input", "write_event", "on_demand", "on_deliver"}
)
VALID_JURISDICTIONS = frozenset({"us", "eu", "kr", "local", "unknown"})
VALID_COMPENSATION_TIERS = frozenset({"t1_clean", "t2_trail", "t3_new_artifact", "t4_irreversible"})
"""Workflow §9.1 four-tier compensation taxonomy."""


class PluginRegistrationError(ValueError):
    """Raised when a plugin declaration violates the framework contract."""


class PluginLoadError(RuntimeError):
    """Raised when the loader cannot discover or import a plugin module."""


class PluginRunError(RuntimeError):
    """Raised when a runtime dispatch fails (missing capability, exec error, etc.)."""


@dataclass
class InboundCapability:
    fn: Callable[..., Any]
    trigger: dict[str, Any]


@dataclass
class OutboundCapability:
    fn: Callable[..., Any]
    artifact_types: tuple[str, ...]
    # Workflow §9.2 — compensation tier declared per artifact_type at outbound
    # registration. ``compensation_supported`` mirrors whether the plugin
    # paired this outbound with an ``@p.compensate`` handler (T1-T3).
    compensation_tier: str | None = None
    compensation_supported: bool = False


@dataclass
class CompensateCapability:
    """Workflow §9.2 — undo handler for one or more delivered artifact_types."""

    fn: Callable[..., Any]
    artifact_types: tuple[str, ...]


@dataclass
class ActionCapability:
    fn: Callable[..., Any]
    name: str
    mcp_exposed: bool = False
    input_schema: dict[str, Any] | None = None


@dataclass
class PluginMeta:
    """All metadata + runtime references for a single plugin."""

    name: str
    version: str
    description: str
    author: str
    data_jurisdiction: str
    credentials: list[dict[str, Any]]

    inbounds: list[InboundCapability] = field(default_factory=list)
    outbounds: list[OutboundCapability] = field(default_factory=list)
    compensates: list[CompensateCapability] = field(default_factory=list)
    actions: dict[str, ActionCapability] = field(default_factory=dict)
    setup_fn: Callable[..., Any] | None = None
