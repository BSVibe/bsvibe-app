"""DeliveryDispatcher — fan a deliverable out to plugin outbound adapters.

Workflow §12.5 #8 (Bundle G — Delivery). The dispatcher is the bridge
between the orchestrator (which mints ``shipped`` deliverables) and the
plugin runner (which talks to external products).

Phase 1 wiring: ``dispatch`` iterates the caller-supplied plugin list,
filters by ``artifact_type`` match (Workflow §6 #2 — outbound capability
declares the artifact_types it accepts), and runs each plugin's
``@p.outbound`` callable via :meth:`backend.plugins.PluginRunner.dispatch_outbound`.
Per-plugin failures aggregate into the :class:`DeliveryResult.actions`
list — one failed plugin does NOT abort the rest. Workflow §3.1.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.delivery.schema import ActionResult, ArtifactType, DeliveryResult
from backend.plugins.base import PluginMeta, PluginRunError
from backend.plugins.runner import PluginRunner

logger = structlog.get_logger(__name__)


class DeliveryDispatcher:
    """Send one deliverable through every subscribed outbound adapter."""

    def __init__(self, runner: PluginRunner | None = None) -> None:
        self._runner = runner or PluginRunner()

    async def dispatch(
        self,
        *,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
        artifact_type: ArtifactType,
        plugins: Iterable[PluginMeta],
        context: Any = None,
        event: Any = None,
    ) -> DeliveryResult:
        """Run outbound fan-out for one deliverable. Soft-fails per plugin."""
        actions: list[ActionResult] = []
        last_error: str | None = None

        for plugin in plugins:
            # Plugins without an outbound for this artifact_type skip silently.
            if not any(artifact_type in cap.artifact_types for cap in plugin.outbounds):
                continue
            try:
                output = await self._runner.dispatch_outbound(
                    plugin,
                    artifact_type=artifact_type,
                    context=context,
                    event=event,
                )
                actions.append(
                    ActionResult(
                        action=f"{plugin.name}:outbound:{artifact_type}",
                        succeeded=True,
                        output=output if isinstance(output, dict) else {"result": output},
                    )
                )
            except PluginRunError as exc:
                logger.warning(
                    "delivery_plugin_failed",
                    workspace_id=str(workspace_id),
                    deliverable_id=str(deliverable_id),
                    plugin=plugin.name,
                    error=str(exc),
                )
                last_error = str(exc)
                actions.append(
                    ActionResult(
                        action=f"{plugin.name}:outbound:{artifact_type}",
                        succeeded=False,
                        error=str(exc),
                    )
                )

        return DeliveryResult(
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,
            actions=actions,
            delivered_at=datetime.now(tz=UTC),
            error=last_error if not any(a.succeeded for a in actions) else None,
        )


__all__ = ["DeliveryDispatcher"]
