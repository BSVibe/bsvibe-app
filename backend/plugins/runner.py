"""PluginRunner — capability-aware dispatcher.

Inbound, outbound, and action capabilities each have their own dispatch
entry point:

- :meth:`dispatch_inbound` — picks the first ``@p.inbound`` whose trigger
  ``type`` matches the requested ``trigger_type``.
- :meth:`dispatch_outbound` — picks the ``@p.outbound`` whose
  ``artifact_types`` includes the requested ``artifact_type``. Overlap is
  forbidden at registration time, so the match is unique.
- :meth:`dispatch_action` — picks the ``@p.action`` registered under the
  requested ``action_name``. If the action declared ``input_schema``, the
  ``kwargs`` payload is validated before the call.
"""

from __future__ import annotations

from typing import Any

import jsonschema
import structlog

from backend.plugins.base import PluginMeta, PluginRunError

logger = structlog.get_logger(__name__)


class PluginRunner:
    def __init__(
        self,
        credential_store: Any | None = None,
        event_bus: Any | None = None,
    ) -> None:
        self._credential_store = credential_store
        self._event_bus = event_bus

    async def dispatch_inbound(
        self,
        meta: PluginMeta,
        *,
        trigger_type: str,
        context: Any,
        payload: Any,
    ) -> Any:
        for cap in meta.inbounds:
            if cap.trigger.get("type") == trigger_type:
                return await self._call(meta, cap.fn, context, payload)
        raise PluginRunError(f"Plugin {meta.name!r}: no inbound for trigger type {trigger_type!r}")

    async def dispatch_outbound(
        self,
        meta: PluginMeta,
        *,
        artifact_type: str,
        context: Any,
        event: Any,
    ) -> Any:
        for cap in meta.outbounds:
            if artifact_type in cap.artifact_types:
                return await self._call(meta, cap.fn, context, event)
        raise PluginRunError(
            f"Plugin {meta.name!r}: no outbound for artifact_type {artifact_type!r}"
        )

    async def dispatch_compensate(
        self,
        meta: PluginMeta,
        *,
        artifact_type: str,
        context: Any,
        handle: Any,
    ) -> Any:
        """Run the ``@p.compensate`` handler whose artifact_types match.

        Workflow §9 — ``handle`` is the ``compensation_handle`` dict the
        original outbound returned. Handlers must be idempotent.
        """
        for cap in meta.compensates:
            if artifact_type in cap.artifact_types:
                return await self._call(meta, cap.fn, context, handle)
        raise PluginRunError(
            f"Plugin {meta.name!r}: no compensate for artifact_type {artifact_type!r}"
        )

    async def dispatch_action(
        self,
        meta: PluginMeta,
        *,
        action_name: str,
        context: Any,
        kwargs: dict[str, Any],
    ) -> Any:
        cap = meta.actions.get(action_name)
        if cap is None:
            raise PluginRunError(f"Plugin {meta.name!r}: no action {action_name!r}")
        if cap.input_schema is not None:
            try:
                jsonschema.validate(instance=kwargs, schema=cap.input_schema)
            except jsonschema.ValidationError as exc:
                raise PluginRunError(
                    f"Plugin {meta.name!r}.{action_name}: input schema validation failed: {exc.message}"
                ) from exc
        return await self._call(meta, cap.fn, context, kwargs=kwargs)

    @staticmethod
    async def _call(
        meta: PluginMeta,
        fn: Any,
        context: Any,
        arg: Any = None,
        *,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        try:
            if kwargs is not None:
                return await fn(context, **kwargs)
            return await fn(context, arg)
        except PluginRunError:
            raise
        except Exception as exc:
            logger.warning("plugin_call_failed", name=meta.name, error=str(exc))
            raise PluginRunError(f"Plugin {meta.name!r} call failed: {exc}") from exc
