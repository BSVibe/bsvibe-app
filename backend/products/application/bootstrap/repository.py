"""Repository Protocol for the bootstrap status row.

Lift A v2 — keeps the application orchestrator free of SQLAlchemy. The
runtime layer (which CAN import SQLAlchemy) wires the concrete repo and
hands it to :func:`run_repo_bootstrap`; the orchestrator just calls the
Protocol methods.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class BootstrapProgress:
    """Snapshot of one Product's bootstrap status for the GET endpoint."""

    product_id: uuid.UUID
    status: str | None
    artifacts_count: int | None
    error: str | None
    run_id: uuid.UUID | None
    started_at: datetime | None
    completed_at: datetime | None


class BootstrapRepository(Protocol):
    """Per-Product status writes the orchestrator needs.

    Each method commits its own transaction — the orchestrator is the only
    caller, and the writes are status-bar updates that the founder UI polls
    out-of-band. Committing per-step keeps the UI's view fresh.
    """

    async def mark_status(
        self,
        product_id: uuid.UUID,
        *,
        status: str,
        run_id: uuid.UUID | None = None,
        artifacts_count: int | None = None,
        error: str | None = None,
    ) -> None:
        """Set ``bootstrap_status`` (+ optional telemetry) on ``product_id``."""

    async def fetch_progress(
        self, product_id: uuid.UUID, *, workspace_id: uuid.UUID
    ) -> BootstrapProgress | None:
        """Read the current progress snapshot; ``None`` when ``product_id``
        is not in ``workspace_id`` (the GET endpoint maps that to 404)."""


__all__ = ["BootstrapProgress", "BootstrapRepository"]
