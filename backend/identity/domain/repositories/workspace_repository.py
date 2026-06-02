"""WorkspaceRepository Protocol — read/write seam for :class:`WorkspaceRow`.

v8 D44/D45. The workspace row is the top-level multi-tenancy unit
(Workflow §3) — it carries ``name``, ``region``, ``safe_mode``,
``legal_basis``, and the GDPR ``deleted_at`` soft-delete marker. Callers
today reach for ``session.get(WorkspaceRow, workspace_id)`` and
``select(WorkspaceRow).where(...)`` directly; this Protocol moves those
queries behind a stable seam.

Method surface limited to what existing callers actually use today (the
founder-facing workspace REST surface, the GDPR compliance export, the
delivery worker's per-workspace bootstrap, the settle worker's per-region
sweep). New methods get added per real caller, never speculatively.

Concrete impl:
:mod:`backend.identity.infrastructure.repositories.workspace_repository_sql`.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from backend.identity.workspaces_db import WorkspaceRow


@runtime_checkable
class WorkspaceRepository(Protocol):
    """Persistence seam for :class:`WorkspaceRow` rows."""

    async def get(self, workspace_id: uuid.UUID) -> WorkspaceRow | None:
        """Return the workspace with this id, or ``None`` if it doesn't exist.

        Does NOT filter ``deleted_at``; callers (e.g. the workspaces router)
        often want to inspect the soft-delete marker on the returned row.
        """

    async def get_live(self, workspace_id: uuid.UUID) -> WorkspaceRow | None:
        """Like :meth:`get` but treats a soft-deleted workspace as gone.

        Returns ``None`` if the row doesn't exist OR ``deleted_at`` is set.
        Used by the workspaces router's 404 path so a non-member cannot
        probe which workspace ids exist.
        """

    async def list_for_user(self, user_id: uuid.UUID) -> list[WorkspaceRow]:
        """Every live workspace this user has an active membership in.

        Ordered by ``WorkspaceRow.created_at`` descending — matches the
        existing PWA "your workspaces" listing. Excludes soft-deleted
        workspaces (``deleted_at IS NULL``).
        """

    async def list_active_regions(self) -> list[tuple[uuid.UUID, str, bool]]:
        """Every live workspace's ``(id, region, safe_mode)`` triple.

        Powers infrastructure-layer sweeps that need to fan out across
        workspaces (e.g. the settle worker's per-region drain). Excludes
        soft-deleted rows.
        """

    async def list_with_audit_retention(self) -> list[tuple[uuid.UUID, int]]:
        """Every live workspace with a non-NULL ``audit_retention_days``.

        Powers the Lift Q1 retention sweep
        (:class:`plugin.audit.retention_sweep.AuditRetentionSweepRunner`).
        NULL-retention workspaces are filtered OUT at the DB level — the
        sweep should never see them, because ``NULL`` means *forever*
        (no deletion). Excludes soft-deleted workspaces.

        The list is bounded by the workspace count (small — tens to
        hundreds), so loading all rows in one query is fine; no batch /
        cursor needed at this scale.
        """

    async def add(self, workspace: WorkspaceRow) -> None:
        """Stage a new workspace for INSERT on the next flush.

        The repository does NOT flush or commit — transaction boundaries
        are owned at the application service / request scope (v8 D45).
        """


__all__ = ["WorkspaceRepository"]
