"""Dispatcher dependency + serialization helpers for ``/api/v1/safemode``.

The ``get_delivery_dispatcher`` builder is re-exported from the package's
``__init__.py`` because the test suite imports it directly for
``app.dependency_overrides`` (see ``tests/glue/test_safe_mode_*.py``). Keeping
the public-surface name on the package preserves those import paths.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import _get_session_factory
from backend.workflow.domain.delivery import ArtifactType
from backend.workflow.infrastructure.db import Deliverable
from backend.workflow.infrastructure.workers.delivery_worker import PluginDispatchAdapter
from backend.workflow.infrastructure.workers.run import build_delivery_adapter

from ._schemas import SafeModeItemResponse


async def get_delivery_dispatcher() -> PluginDispatchAdapter:
    """The outbound dispatcher used when a queued delivery is approved.

    Builds the SAME :class:`~backend.workflow.application.delivery.connector_dispatch.ConnectorDeliveryAdapter`
    the Direct path uses (``backend.workflow.infrastructure.workers.run.build_delivery_adapter``): it
    loads every connector plugin, carries the settings-derived
    :class:`~backend.router.accounts.crypto.CredentialCipher`, and opens its own
    session per dispatch (it resolves the workspace's ``connector_accounts``
    delivery binding itself). So an approved delivery shapes + delivers the
    connector outbound event exactly as a Safe-Mode-off delivery does — one
    outbound code path, no connector-shaping duplication.

    The adapter carries the process-wide session factory rather than the
    request-scoped session because it must open a session per dispatch (load the
    Deliverable + resolve the binding). Tests override this dependency to inject
    a connector adapter built against the test session factory, so both code
    paths converge on one adapter.
    """
    return await build_delivery_adapter(session_factory=_get_session_factory())


def _to_item_response(item: object) -> SafeModeItemResponse:
    """Map a :class:`SafeModeQueueItemRow` to the response shape (B12a — also
    threads ``run_id`` through)."""
    return SafeModeItemResponse(
        id=item.id,  # type: ignore[attr-defined]
        workspace_id=item.workspace_id,  # type: ignore[attr-defined]
        deliverable_id=item.deliverable_id,  # type: ignore[attr-defined]
        run_id=item.run_id,  # type: ignore[attr-defined]
        status=item.status.value,  # type: ignore[attr-defined]
        compensation_tier=None,
        expires_at=item.expires_at,  # type: ignore[attr-defined]
        extension_count=item.extension_count,  # type: ignore[attr-defined]
        created_at=item.created_at,  # type: ignore[attr-defined]
    )


async def _artifact_type_for(session: AsyncSession, deliverable_id: uuid.UUID) -> ArtifactType:
    """Resolve the deliverable's artifact_type for the dispatch call.

    ``DeliverableType`` values mirror the ``ArtifactType`` literals 1:1; we
    fall back to ``direct_output`` if the deliverable row is gone (the queue
    item still carries the id, but the run could have been purged).
    """
    deliverable = await session.get(Deliverable, deliverable_id)
    if deliverable is None:
        return "direct_output"
    value: str = deliverable.deliverable_type.value
    return value  # type: ignore[return-value]


__all__ = [
    "_artifact_type_for",
    "_to_item_response",
    "get_delivery_dispatcher",
]
