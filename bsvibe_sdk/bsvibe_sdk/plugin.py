# bsvibe:stable-internal — modifications require a design doc update.
# Owners: bsvibe_sdk
"""Plugin Protocol, capability records, validation, and the ``plugin(...)`` builder.

Lift R2b unification: this module is the canonical home of the
``PluginBuilder`` class and the ``plugin(...)`` factory. The engine's
historical ``backend.extensions.plugin.decorator`` module is now a thin
re-export of the symbols defined here, and every connector (and the engine
loader/runner) consumes the same single class.

Per v8 §13 D38/D39/D42 the SDK stays dependency-light — this module uses
only the standard library, so the eventual external publication of
``bsvibe_sdk`` on PyPI carries no backend coupling. The author-facing
validations live here so plugin authors get the same protection whether
they install ``bsvibe_sdk`` standalone or run inside the BSVibe engine.

The SDK is currently at v0.1.0 (unpublished). Lift R2b promotes
``PluginBuilder`` / ``PluginMeta`` / capability dataclasses onto the
public surface — all data-only types that any future external publication
will keep as the stable contract.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# --------------------------------------------------------------------------- #
# Validation constants — Workflow §6 #4 + §9.1.                                #
# --------------------------------------------------------------------------- #

VALID_TRIGGER_TYPES: frozenset[str] = frozenset(
    {"cron", "webhook", "on_input", "write_event", "on_demand", "on_deliver"}
)
"""Trigger ``type`` values accepted on ``@p.inbound``."""

VALID_JURISDICTIONS: frozenset[str] = frozenset({"us", "eu", "kr", "local", "unknown"})
"""``data_jurisdiction`` values accepted on ``plugin(...)``."""

VALID_COMPENSATION_TIERS: frozenset[str] = frozenset(
    {"t1_clean", "t2_trail", "t3_new_artifact", "t4_irreversible"}
)
"""Workflow §9.1 four-tier compensation taxonomy."""


class PluginRegistrationError(ValueError):
    """Raised when a plugin declaration violates the SDK contract.

    Historical alias: ``PluginDeclarationError``. Re-exported under the
    legacy name below for the very few callers that imported it.
    """


# Back-compat alias — Lift S originally introduced this name; nothing in
# the live codebase uses it but we keep the symbol so external authors who
# adopted the SDK pre-R2b don't break on upgrade.
PluginDeclarationError = PluginRegistrationError


# --------------------------------------------------------------------------- #
# Capability dataclasses — runner consumes these directly.                     #
# --------------------------------------------------------------------------- #


@dataclass
class InboundCapability:
    fn: Callable[..., Awaitable[Any]]
    trigger: dict[str, Any]


@dataclass
class OutboundCapability:
    fn: Callable[..., Awaitable[Any]]
    artifact_types: tuple[str, ...]
    # Workflow §9.2 — compensation tier declared per artifact_type at outbound
    # registration. ``compensation_supported`` mirrors whether the plugin paired
    # this outbound with an ``@p.compensate`` handler (T1-T3).
    compensation_tier: str | None = None
    compensation_supported: bool = False


@dataclass
class CompensateCapability:
    """Workflow §9.2 — undo handler for one or more delivered artifact_types."""

    fn: Callable[..., Awaitable[Any]]
    artifact_types: tuple[str, ...]


@dataclass
class ActionCapability:
    fn: Callable[..., Awaitable[Any]]
    name: str
    mcp_exposed: bool = False
    input_schema: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# PluginMeta — the canonical loaded-plugin record. Engine PluginRunner +       #
# registry consume it directly; ``backend.extensions.plugin.base.PluginMeta``  #
# is now an alias of this symbol.                                              #
# --------------------------------------------------------------------------- #


@dataclass
class PluginMeta:
    """All metadata + runtime references for a single plugin.

    Authors do not construct this directly — they go through
    :func:`plugin` and the capability decorators on the returned
    :class:`PluginBuilder`, which mutate ``self.meta`` in place.
    """

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
    setup_fn: Callable[..., Awaitable[Any]] | None = None


@runtime_checkable
class Plugin(Protocol):
    """A declared plugin instance.

    Concrete carrier is :class:`PluginMeta`. This Protocol publishes only
    the surface used by tooling that introspects loaded plugins
    (e.g. MCP listings, admin UIs).
    """

    name: str

    def list_actions(self) -> list[str]: ...


# --------------------------------------------------------------------------- #
# PluginBuilder — the author-facing API.                                       #
# --------------------------------------------------------------------------- #


class PluginBuilder:
    """Holds a :class:`PluginMeta` and exposes the capability decorators.

    Returned by :func:`plugin`. Plugin authors call ``@p.inbound(...)``,
    ``@p.outbound(...)``, ``@p.action(...)``, ``@p.compensate(...)``,
    ``@p.setup`` on the returned instance to register capabilities.
    """

    def __init__(self, *, meta: PluginMeta) -> None:
        self.meta = meta

    @property
    def name(self) -> str:
        return self.meta.name

    def list_actions(self) -> list[str]:
        return list(self.meta.actions.keys())

    # ----------------------------------------------------------------- inbound
    def inbound(
        self, *, trigger: dict[str, Any]
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        trigger_type = trigger.get("type") if isinstance(trigger, dict) else None
        if not trigger_type:
            raise PluginRegistrationError(
                f"Plugin {self.meta.name!r}: @inbound trigger missing 'type'"
            )
        if trigger_type not in VALID_TRIGGER_TYPES:
            raise PluginRegistrationError(
                f"Plugin {self.meta.name!r}: invalid trigger type {trigger_type!r}; "
                f"must be one of {sorted(VALID_TRIGGER_TYPES)}"
            )

        def register(
            fn: Callable[..., Awaitable[Any]],
        ) -> Callable[..., Awaitable[Any]]:
            self.meta.inbounds.append(InboundCapability(fn=fn, trigger=dict(trigger)))
            return fn

        return register

    # ---------------------------------------------------------------- outbound
    def outbound(
        self,
        *,
        artifact_types: list[str],
        compensation_tier: str | None = None,
        compensation_supported: bool = False,
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        if not artifact_types:
            raise PluginRegistrationError(
                f"Plugin {self.meta.name!r}: @outbound artifact_types must be non-empty"
            )
        if compensation_tier is not None and compensation_tier not in VALID_COMPENSATION_TIERS:
            raise PluginRegistrationError(
                f"Plugin {self.meta.name!r}: invalid compensation_tier {compensation_tier!r}; "
                f"must be one of {sorted(VALID_COMPENSATION_TIERS)}"
            )
        ats = tuple(artifact_types)
        existing = {t for cap in self.meta.outbounds for t in cap.artifact_types}
        overlap = existing & set(ats)
        if overlap:
            raise PluginRegistrationError(
                f"Plugin {self.meta.name!r}: @outbound artifact_type overlap: {sorted(overlap)}"
            )

        def register(
            fn: Callable[..., Awaitable[Any]],
        ) -> Callable[..., Awaitable[Any]]:
            self.meta.outbounds.append(
                OutboundCapability(
                    fn=fn,
                    artifact_types=ats,
                    compensation_tier=compensation_tier,
                    compensation_supported=compensation_supported,
                )
            )
            return fn

        return register

    # ------------------------------------------------------------- compensate
    def compensate(
        self, *, artifact_types: list[str]
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        """Register an undo handler for one or more delivered artifact_types.

        Workflow §9.2 — pairs with ``@p.outbound`` for tiers T1-T3. The
        handler receives ``(context, handle)`` where ``handle`` is the
        ``compensation_handle`` dict the matching outbound returned, and must
        be idempotent (re-call after success is a silent no-op).
        """
        if not artifact_types:
            raise PluginRegistrationError(
                f"Plugin {self.meta.name!r}: @compensate artifact_types must be non-empty"
            )
        ats = tuple(artifact_types)
        existing = {t for cap in self.meta.compensates for t in cap.artifact_types}
        overlap = existing & set(ats)
        if overlap:
            raise PluginRegistrationError(
                f"Plugin {self.meta.name!r}: @compensate artifact_type overlap: {sorted(overlap)}"
            )

        def register(
            fn: Callable[..., Awaitable[Any]],
        ) -> Callable[..., Awaitable[Any]]:
            self.meta.compensates.append(CompensateCapability(fn=fn, artifact_types=ats))
            return fn

        return register

    # ------------------------------------------------------------------ action
    def action(
        self,
        *,
        name: str,
        mcp_exposed: bool = False,
        input_schema: dict[str, Any] | None = None,
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        if not name:
            raise PluginRegistrationError(
                f"Plugin {self.meta.name!r}: @action requires non-empty name"
            )
        if name in self.meta.actions:
            raise PluginRegistrationError(
                f"Plugin {self.meta.name!r}: action {name!r} already registered"
            )

        def register(
            fn: Callable[..., Awaitable[Any]],
        ) -> Callable[..., Awaitable[Any]]:
            self.meta.actions[name] = ActionCapability(
                fn=fn, name=name, mcp_exposed=mcp_exposed, input_schema=input_schema
            )
            return fn

        return register

    # ------------------------------------------------------------------- setup
    def setup(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if self.meta.setup_fn is not None:
            raise PluginRegistrationError(f"Plugin {self.meta.name!r}: @setup already registered")
        self.meta.setup_fn = fn
        return fn


# --------------------------------------------------------------------------- #
# Factory.                                                                    #
# --------------------------------------------------------------------------- #


def plugin(
    *,
    name: str,
    credentials: list[dict[str, Any]],
    data_jurisdiction: str,
    version: str = "0.1.0",
    description: str = "",
    author: str = "",
) -> PluginBuilder:
    """Declare a plugin. Returns a :class:`PluginBuilder`.

    Example::

        from bsvibe_sdk import plugin

        p = plugin(name="github", credentials=[...], data_jurisdiction="us")

        @p.action(name="open_pr", mcp_exposed=True)
        async def open_pr(context, *, branch, title, body): ...
    """
    if not _NAME_RE.match(name):
        raise PluginRegistrationError(
            f"Invalid plugin name {name!r}: must match {_NAME_RE.pattern}"
        )
    if data_jurisdiction not in VALID_JURISDICTIONS:
        raise PluginRegistrationError(
            f"Invalid data_jurisdiction {data_jurisdiction!r}: "
            f"must be one of {sorted(VALID_JURISDICTIONS)}"
        )
    meta = PluginMeta(
        name=name,
        version=version,
        description=description,
        author=author,
        data_jurisdiction=data_jurisdiction,
        credentials=list(credentials),
    )
    return PluginBuilder(meta=meta)


__all__ = [
    "VALID_COMPENSATION_TIERS",
    "VALID_JURISDICTIONS",
    "VALID_TRIGGER_TYPES",
    "ActionCapability",
    "CompensateCapability",
    "InboundCapability",
    "OutboundCapability",
    "Plugin",
    "PluginBuilder",
    "PluginDeclarationError",
    "PluginMeta",
    "PluginRegistrationError",
    "plugin",
]
