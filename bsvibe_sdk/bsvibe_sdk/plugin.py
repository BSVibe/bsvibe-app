"""Plugin Protocol + the public ``plugin(...)`` builder factory.

This is the SDK-facing equivalent of the engine builder at
``backend.extensions.plugin.decorator`` — plugin authors import
``plugin`` from ``bsvibe_sdk`` and declare capabilities via the returned
:class:`PluginBuilder`. The engine loader (Lift S keeps this internally
at ``backend.extensions.plugin.loader``) understands both the SDK
builder and the historical engine builder; both publish identically
shaped :class:`PluginSpec` records.

Per v8 §D42 the SDK stays dependency-light: this module uses only the
standard library, so the eventual external publication of ``bsvibe_sdk``
on PyPI carries no backend coupling.

Lift S keeps the engine's richer registration validation
(trigger types, jurisdictions, compensation tiers) inside
``backend.extensions.plugin.base``; the SDK builder publishes the
minimal author-facing surface. The engine adapts.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class PluginDeclarationError(ValueError):
    """Raised when a plugin declaration violates the SDK contract."""


@runtime_checkable
class Plugin(Protocol):
    """A declared plugin instance.

    Concrete engine carrier is
    :class:`backend.extensions.plugin.base.PluginMeta`. This Protocol
    publishes only the surface used by tooling that introspects loaded
    plugins (e.g. MCP listings, admin UIs).
    """

    name: str

    def list_actions(self) -> list[str]: ...


@dataclass
class _Capability:
    """Author-declared capability record. Engine reads + validates these."""

    kind: str  # one of: "inbound", "outbound", "action", "compensate", "setup"
    fn: Callable[..., Awaitable[Any]]
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class PluginSpec:
    """Public, dependency-free plugin declaration.

    Carries the data the engine loader needs to materialize a runtime
    :class:`backend.extensions.plugin.base.PluginMeta`. Authors do not
    construct this directly — they go through :func:`plugin`.
    """

    name: str
    version: str
    description: str
    author: str
    data_jurisdiction: str
    credentials: list[dict[str, Any]]
    capabilities: list[_Capability] = field(default_factory=list)

    def list_actions(self) -> list[str]:
        return [
            cap.options["name"]
            for cap in self.capabilities
            if cap.kind == "action" and "name" in cap.options
        ]


class PluginBuilder:
    """Returned by :func:`plugin`. Exposes capability decorators."""

    def __init__(self, *, spec: PluginSpec) -> None:
        self.spec = spec

    @property
    def name(self) -> str:
        return self.spec.name

    def list_actions(self) -> list[str]:
        return self.spec.list_actions()

    # ----------------------------------------------------------------- inbound
    def inbound(
        self, *, trigger: dict[str, Any]
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        if not isinstance(trigger, dict) or not trigger.get("type"):
            raise PluginDeclarationError(
                f"Plugin {self.spec.name!r}: @inbound trigger missing 'type'"
            )

        def register(
            fn: Callable[..., Awaitable[Any]],
        ) -> Callable[..., Awaitable[Any]]:
            self.spec.capabilities.append(
                _Capability(kind="inbound", fn=fn, options={"trigger": dict(trigger)})
            )
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
            raise PluginDeclarationError(
                f"Plugin {self.spec.name!r}: @outbound artifact_types must be non-empty"
            )

        def register(
            fn: Callable[..., Awaitable[Any]],
        ) -> Callable[..., Awaitable[Any]]:
            self.spec.capabilities.append(
                _Capability(
                    kind="outbound",
                    fn=fn,
                    options={
                        "artifact_types": tuple(artifact_types),
                        "compensation_tier": compensation_tier,
                        "compensation_supported": compensation_supported,
                    },
                )
            )
            return fn

        return register

    # ------------------------------------------------------------- compensate
    def compensate(
        self, *, artifact_types: list[str]
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        if not artifact_types:
            raise PluginDeclarationError(
                f"Plugin {self.spec.name!r}: @compensate artifact_types must be non-empty"
            )

        def register(
            fn: Callable[..., Awaitable[Any]],
        ) -> Callable[..., Awaitable[Any]]:
            self.spec.capabilities.append(
                _Capability(
                    kind="compensate",
                    fn=fn,
                    options={"artifact_types": tuple(artifact_types)},
                )
            )
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
            raise PluginDeclarationError(
                f"Plugin {self.spec.name!r}: @action requires non-empty name"
            )
        if any(
            cap.kind == "action" and cap.options.get("name") == name
            for cap in self.spec.capabilities
        ):
            raise PluginDeclarationError(
                f"Plugin {self.spec.name!r}: action {name!r} already registered"
            )

        def register(
            fn: Callable[..., Awaitable[Any]],
        ) -> Callable[..., Awaitable[Any]]:
            self.spec.capabilities.append(
                _Capability(
                    kind="action",
                    fn=fn,
                    options={
                        "name": name,
                        "mcp_exposed": mcp_exposed,
                        "input_schema": input_schema,
                    },
                )
            )
            return fn

        return register

    # ------------------------------------------------------------------- setup
    def setup(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if any(cap.kind == "setup" for cap in self.spec.capabilities):
            raise PluginDeclarationError(f"Plugin {self.spec.name!r}: @setup already registered")
        self.spec.capabilities.append(_Capability(kind="setup", fn=fn))
        return fn


def plugin(
    *,
    name: str,
    credentials: list[dict[str, Any]],
    data_jurisdiction: str,
    version: str = "0.1.0",
    description: str = "",
    author: str = "",
) -> PluginBuilder:
    """Declare a plugin. Returns a :class:`PluginBuilder` with capability decorators.

    Example::

        from bsvibe_sdk import plugin

        p = plugin(name="github", credentials=[...], data_jurisdiction="us")

        @p.action(name="open_pr", mcp_exposed=True)
        async def open_pr(context, *, branch, title, body): ...
    """
    if not _NAME_RE.match(name):
        raise PluginDeclarationError(f"Invalid plugin name {name!r}: must match {_NAME_RE.pattern}")
    spec = PluginSpec(
        name=name,
        version=version,
        description=description,
        author=author,
        data_jurisdiction=data_jurisdiction,
        credentials=list(credentials),
    )
    return PluginBuilder(spec=spec)


__all__ = ["Plugin", "PluginBuilder", "PluginDeclarationError", "PluginSpec", "plugin"]
