"""ResourceBindingRepository Protocol — read/write seam for ``resource_bindings``.

Lift I-Repo-Final Phase A. The 3-knob *Resource binding* (Workflow §3) row
carries ``selection`` / ``trigger`` / ``output_mode`` for one Product ×
ConnectorAccount pairing. Before this lift, the only seam was the concrete
``backend.workspaces.resource_bindings.ResourceBindingRepository`` class
that exposed both the Protocol shape AND the SQLAlchemy access in one
module. Absorbing :mod:`backend.workspaces` into :mod:`backend.identity`
splits it into the Protocol here + the concrete
:class:`SqlAlchemyResourceBindingRepository` under
:mod:`backend.identity.infrastructure.repositories` (matches the Lift
I-Repo-Identity convention for Workspace / User / Membership).

Method surface preserves every caller's contract verbatim:

* :meth:`create` — workspace-scoped insert (validates ``output_mode``).
* :meth:`get` — workspace-scoped lookup by binding id.
* :meth:`list_for_product` — ordered listing of one Product's bindings.
* :meth:`update` — patch a subset of the 3 knobs (``None`` = leave as-is).
* :meth:`delete` — hard-delete; returns ``False`` if not present.
* :meth:`find_binding` — the Receive-stage lookup (no ``workspace_id``
  parameter — the inbound path already resolved the account to a workspace
  upstream and the binding's ``workspace_id`` matches by construction).

Concrete impl:
:mod:`backend.identity.infrastructure.repositories.resource_binding_repository_sql`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from backend.identity.workspaces_db import ResourceBindingRow

# Allowed values for the ``output_mode`` knob (Workflow §3 / §1). Kept here so
# the Protocol module is the single import source for both the seam type AND
# the wire-validated value set callers + tests rely on (mirrors the old
# ``backend.workspaces.resource_bindings`` import surface).
OUTPUT_MODES: frozenset[str] = frozenset({"safe", "direct"})


@runtime_checkable
class ResourceBindingRepository(Protocol):
    """Persistence seam for ``resource_bindings`` rows."""

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        product_id: uuid.UUID,
        connector_account_id: uuid.UUID,
        resource_id: str,
        selection: dict[str, Any] | None = ...,
        trigger: dict[str, Any] | None = ...,
        output_mode: str = ...,
    ) -> ResourceBindingRow:
        """Insert a new binding; validates ``output_mode`` against ``OUTPUT_MODES``."""

    async def get(
        self, *, workspace_id: uuid.UUID, binding_id: uuid.UUID
    ) -> ResourceBindingRow | None:
        """Workspace-scoped lookup by binding id."""

    async def list_for_product(
        self, *, workspace_id: uuid.UUID, product_id: uuid.UUID
    ) -> Sequence[ResourceBindingRow]:
        """Workspace-scoped listing of one Product's bindings, oldest-first."""

    async def update(
        self,
        row: ResourceBindingRow,
        *,
        selection: dict[str, Any] | None = ...,
        trigger: dict[str, Any] | None = ...,
        output_mode: str | None = ...,
    ) -> ResourceBindingRow:
        """Patch a subset of the 3 knobs (``None`` = leave as-is)."""

    async def delete(self, *, workspace_id: uuid.UUID, binding_id: uuid.UUID) -> bool:
        """Hard-delete the binding. Returns ``False`` if not present or other workspace."""

    async def find_binding(
        self, *, connector_account_id: uuid.UUID, resource_id: str
    ) -> ResourceBindingRow | None:
        """Receive-stage lookup — resolve ``(account, resource)`` to a binding (or ``None``)."""


__all__ = ["OUTPUT_MODES", "ResourceBindingRepository"]
