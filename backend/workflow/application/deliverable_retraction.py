"""Retracting a delivered artifact — the rule, shared by REST and MCP.

Rolling a delivered direct-mode artifact back means calling each originating
plugin's ``@p.compensate`` handler with the handle captured at delivery time,
and flipping ``retracted_at`` only once every handler has succeeded.

Two things live apart on purpose:

* **The rule** (order, idempotency, the all-or-nothing flip) is here, in the
  context that owns ``Deliverable``.
* **The runtime that actually calls a plugin** is a :class:`RetractHandler`
  supplied by the caller. Its production implementation reaches
  ``backend.extensions`` / ``backend.connectors`` / ``backend.router``, which
  the import contract forbids the MCP context — so the composition root
  injects it rather than either surface importing it.

Copying the rule into the MCP module instead would satisfy the parity guard
while creating exactly the drift that guard exists to catch: the two surfaces
would each own a copy of "when is it safe to mark this retracted".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.domain.repositories import DeliverableRepository

logger = structlog.get_logger(__name__)


class RetractHandler(Protocol):
    """The runtime hand-off that actually calls a plugin's ``@p.compensate``."""

    async def compensate(
        self,
        *,
        plugin: str,
        artifact_type: str,
        handle: dict[str, Any],
        workspace_id: uuid.UUID,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CompensationEntry:
    """One per-stored-handle dispatch outcome (Workflow §3.1)."""

    plugin: str
    artifact_type: str
    output: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetractOutcome:
    """Result of :func:`retract_deliverable`.

    Exactly one of the flags describes what happened, so each surface spells
    the same four outcomes in its own vocabulary (REST 404/400/502/200, MCP
    ``ToolError``) without either one re-deriving the rule:

    ``found=False``            unknown id, or another workspace's row
    ``no_handles=True``        nothing was captured to revert
    ``failure`` is not None    a compensate raised — row NOT marked retracted
    otherwise                  retracted (``already_retracted`` says whether
                               this call is the one that did it)
    """

    found: bool
    already_retracted: bool = False
    no_handles: bool = False
    failure: str | None = None
    retracted_at: datetime | None = None
    compensated: list[CompensationEntry] = field(default_factory=list)


async def retract_deliverable(
    session: AsyncSession,
    deliverables: DeliverableRepository,
    *,
    deliverable_id: uuid.UUID,
    workspace_id: uuid.UUID,
    handler: RetractHandler,
) -> RetractOutcome:
    """Revert a delivered artifact, then mark the row retracted.

    The flip is all-or-nothing: a single failing compensate leaves
    ``retracted_at`` unset so the operator can retry (plugin handlers are
    idempotent and re-tolerate the second call). Re-retracting an already
    retracted row is a short-circuit no-op — the handlers are not fired twice.
    """
    row = await deliverables.get(deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        # Existence is never leaked across the workspace boundary.
        return RetractOutcome(found=False)

    if row.retracted_at is not None:
        return RetractOutcome(found=True, already_retracted=True, retracted_at=row.retracted_at)

    handles = list(row.compensation_handles or [])
    if not handles:
        return RetractOutcome(found=True, no_handles=True)

    compensated: list[CompensationEntry] = []
    for entry in handles:
        plugin = str(entry.get("plugin") or "")
        artifact_type = str(entry.get("artifact_type") or "")
        handle = entry.get("handle")
        if not plugin or not isinstance(handle, dict):
            # Malformed stored entry — surfaced, never silently skipped: it
            # stands for a delivered artifact nobody reverted.
            logger.warning(
                "retract_malformed_entry", deliverable_id=str(deliverable_id), entry=entry
            )
            return RetractOutcome(found=True, failure=f"malformed compensation entry {entry!r}")
        try:
            output = await handler.compensate(
                plugin=plugin,
                artifact_type=artifact_type,
                handle=handle,
                workspace_id=workspace_id,
            )
        except Exception as exc:  # noqa: BLE001 — reported; the row stays un-retracted
            logger.warning(
                "retract_compensate_failed",
                deliverable_id=str(deliverable_id),
                plugin=plugin,
                artifact_type=artifact_type,
                error=str(exc),
            )
            return RetractOutcome(found=True, failure=str(exc))
        compensated.append(
            CompensationEntry(
                plugin=plugin,
                artifact_type=artifact_type,
                output=output if isinstance(output, dict) else {"result": output},
            )
        )

    now = datetime.now(tz=UTC)
    row.retracted_at = now
    await session.commit()
    logger.info(
        "deliverable_retracted",
        deliverable_id=str(deliverable_id),
        workspace_id=str(workspace_id),
        compensated=len(compensated),
    )
    return RetractOutcome(found=True, retracted_at=now, compensated=compensated)


__all__ = [
    "CompensationEntry",
    "RetractHandler",
    "RetractOutcome",
    "retract_deliverable",
]
