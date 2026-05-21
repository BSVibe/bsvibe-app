"""DeliveryDispatcher — fan a deliverable out to plugin outbound adapters.

Workflow §12.5 #8 (Bundle G — Delivery). The dispatcher is the bridge
between the orchestrator (which mints ``shipped`` deliverables) and
the plugin runner (which talks to external products).
"""

from __future__ import annotations

import uuid

import structlog

from backend.delivery.schema import ArtifactType, DeliveryResult

logger = structlog.get_logger(__name__)


class DeliveryDispatcher:
    """Send one deliverable through every subscribed outbound adapter.

    Per Workflow §6 #2, plugin adapters subscribe to ``artifact_type``
    tags. The dispatcher resolves the subscriber set, invokes each
    adapter in parallel, and aggregates the per-action results.
    """

    async def dispatch(
        self,
        *,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
        artifact_type: ArtifactType,
    ) -> DeliveryResult:
        """Run the full outbound fan-out for one deliverable."""
        # TODO(bundle-g-integration): wire
        # backend.plugins.PluginRunner.dispatch_outbound — iterate the
        # subscriber list for ``artifact_type``, gather ActionResults,
        # persist the DeliveryEventRow, return DeliveryResult.
        logger.debug(
            "delivery_dispatcher_stub",
            workspace_id=str(workspace_id),
            deliverable_id=str(deliverable_id),
            artifact_type=artifact_type,
        )
        raise NotImplementedError("DeliveryDispatcher.dispatch pending Bundle G integration")


__all__ = ["DeliveryDispatcher"]
