"""ResourceBindingRepository — CRUD + Receive lookup for the 3-knob binding.

A *Resource binding* (Workflow §3) is the per-Product × ConnectorAccount row
carrying ``selection`` / ``trigger`` / ``output_mode``. The repository is the
single typed surface for working with bindings:

* :meth:`create` / :meth:`get` / :meth:`list_for_product` / :meth:`update` /
  :meth:`delete` — workspace-scoped CRUD (every read/mutation filters on
  ``workspace_id`` so cross-workspace access is impossible at the SQL layer).
* :meth:`find_binding` — the Receive-stage lookup the B10b inbound path will
  call: given ``(connector_account_id, resource_id)`` return the binding (or
  ``None`` — a miss is not an error). This is the index the schema's
  ``ix_resource_bindings_lookup`` is shaped for.

Side note on output_mode validation: TEXT column + app-side validation (here +
in the API schema) rather than a Postgres ENUM keeps the SQLite test tier
honest and dodges the alembic-enum-create-type traps (no DROP-TYPE / no double
``CREATE TYPE`` on migration rerun).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workspaces.db import ResourceBindingRow

# Allowed values for the ``output_mode`` knob (Workflow §3 / §1).
OUTPUT_MODES: frozenset[str] = frozenset({"safe", "direct"})


def _validate_output_mode(value: str) -> str:
    if value not in OUTPUT_MODES:
        raise ValueError(f"output_mode must be one of {sorted(OUTPUT_MODES)}, got {value!r}")
    return value


def _default_trigger() -> dict[str, Any]:
    return {"enabled": False, "filters": {}}


class ResourceBindingRepository:
    """SQL CRUD + Receive lookup for ``resource_bindings`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        product_id: uuid.UUID,
        connector_account_id: uuid.UUID,
        resource_id: str,
        selection: dict[str, Any] | None = None,
        trigger: dict[str, Any] | None = None,
        output_mode: str = "safe",
    ) -> ResourceBindingRow:
        _validate_output_mode(output_mode)
        row = ResourceBindingRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=product_id,
            connector_account_id=connector_account_id,
            resource_id=resource_id,
            selection=dict(selection) if selection is not None else {},
            trigger=dict(trigger) if trigger is not None else _default_trigger(),
            output_mode=output_mode,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(
        self, *, workspace_id: uuid.UUID, binding_id: uuid.UUID
    ) -> ResourceBindingRow | None:
        stmt = select(ResourceBindingRow).where(
            ResourceBindingRow.id == binding_id,
            ResourceBindingRow.workspace_id == workspace_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_product(
        self, *, workspace_id: uuid.UUID, product_id: uuid.UUID
    ) -> Sequence[ResourceBindingRow]:
        stmt = (
            select(ResourceBindingRow)
            .where(
                ResourceBindingRow.workspace_id == workspace_id,
                ResourceBindingRow.product_id == product_id,
            )
            .order_by(ResourceBindingRow.created_at.asc())
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def update(
        self,
        row: ResourceBindingRow,
        *,
        selection: dict[str, Any] | None = None,
        trigger: dict[str, Any] | None = None,
        output_mode: str | None = None,
    ) -> ResourceBindingRow:
        """Apply the provided knob updates to ``row``.

        ``None`` means "leave as-is" — so callers can patch a single knob
        without shipping the others. A dict knob value is REPLACED, not merged
        (the caller decided the new shape).
        """
        if output_mode is not None:
            _validate_output_mode(output_mode)
            row.output_mode = output_mode
        if selection is not None:
            row.selection = dict(selection)
        if trigger is not None:
            row.trigger = dict(trigger)
        await self._session.flush()
        return row

    async def delete(self, *, workspace_id: uuid.UUID, binding_id: uuid.UUID) -> bool:
        """Hard-delete the binding. Returns False if not present (or in another workspace)."""
        row = await self.get(workspace_id=workspace_id, binding_id=binding_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def find_binding(
        self, *, connector_account_id: uuid.UUID, resource_id: str
    ) -> ResourceBindingRow | None:
        """Receive-stage lookup — resolve an inbound (account, resource) to a binding.

        Returns ``None`` on a miss (the inbound path turns that into "no
        product bound for this resource — skip" without raising). Used by B10b;
        does NOT take ``workspace_id`` because the inbound trail already
        resolved the account to a workspace upstream (the binding's
        ``workspace_id`` matches the account's by construction).
        """
        stmt = select(ResourceBindingRow).where(
            ResourceBindingRow.connector_account_id == connector_account_id,
            ResourceBindingRow.resource_id == resource_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


__all__ = ["OUTPUT_MODES", "ResourceBindingRepository"]
