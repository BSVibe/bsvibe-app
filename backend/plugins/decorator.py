"""The ``plugin(...)`` factory + per-plugin capability decorators.

Usage (Workflow §6 #4 verbatim)::

    p = plugin(name="github", credentials=[...], data_jurisdiction="us")

    @p.inbound(trigger={"type": "webhook"})
    async def on_webhook(context, payload): ...

    @p.outbound(artifact_types=["code", "pr"])
    async def deliver_pr(context, event): ...

    @p.action(name="open_pr", mcp_exposed=True)
    async def open_pr(context, branch, title, body): ...

    @p.setup
    async def setup(cred_store): ...
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from backend.plugins.base import (
    VALID_COMPENSATION_TIERS,
    VALID_JURISDICTIONS,
    VALID_TRIGGER_TYPES,
    ActionCapability,
    CompensateCapability,
    InboundCapability,
    OutboundCapability,
    PluginMeta,
    PluginRegistrationError,
)

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def plugin(
    *,
    name: str,
    credentials: list[dict[str, Any]],
    data_jurisdiction: str,
    version: str = "0.1.0",
    description: str = "",
    author: str = "",
) -> PluginBuilder:
    """Construct a :class:`PluginBuilder` whose decorators register capabilities."""
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


class PluginBuilder:
    """Holds a :class:`PluginMeta` and exposes the capability decorators."""

    def __init__(self, *, meta: PluginMeta) -> None:
        self.meta = meta

    def inbound(
        self, *, trigger: dict[str, Any]
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        trigger_type = trigger.get("type")
        if not trigger_type:
            raise PluginRegistrationError(
                f"Plugin {self.meta.name!r}: @inbound trigger missing 'type'"
            )
        if trigger_type not in VALID_TRIGGER_TYPES:
            raise PluginRegistrationError(
                f"Plugin {self.meta.name!r}: invalid trigger type {trigger_type!r}; "
                f"must be one of {sorted(VALID_TRIGGER_TYPES)}"
            )

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.meta.inbounds.append(InboundCapability(fn=fn, trigger=dict(trigger)))
            return fn

        return register

    def outbound(
        self,
        *,
        artifact_types: list[str],
        compensation_tier: str | None = None,
        compensation_supported: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
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

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
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

    def compensate(
        self, *, artifact_types: list[str]
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
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

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.meta.compensates.append(CompensateCapability(fn=fn, artifact_types=ats))
            return fn

        return register

    def action(
        self,
        *,
        name: str,
        mcp_exposed: bool = False,
        input_schema: dict[str, Any] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        if name in self.meta.actions:
            raise PluginRegistrationError(
                f"Plugin {self.meta.name!r}: action {name!r} already registered"
            )

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.meta.actions[name] = ActionCapability(
                fn=fn, name=name, mcp_exposed=mcp_exposed, input_schema=input_schema
            )
            return fn

        return register

    def setup(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        if self.meta.setup_fn is not None:
            raise PluginRegistrationError(f"Plugin {self.meta.name!r}: @setup already registered")
        self.meta.setup_fn = fn
        return fn
