"""SqlAlchemyResourceBindingRepository — concrete over one AsyncSession.

Lift I-Repo-Final Phase A. Concrete impl of
:class:`~backend.identity.domain.repositories.resource_binding_repository.ResourceBindingRepository`
backed by SQLAlchemy. One instance per request / worker tick (sharing the
session that owns the transaction boundary). All SQLAlchemy concerns live
here; callers see only the Protocol.

Behaviour is a verbatim port of the legacy
``backend.workspaces.resource_bindings.ResourceBindingRepository`` (which
mixed the Protocol shape + SQL into one class). The split into Protocol +
concrete matches the Lift I-Repo-Identity convention.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.domain.repositories.resource_binding_repository import (
    OUTPUT_MODES,
)
from backend.identity.workspaces_db import ResourceBindingRow


def _validate_output_mode(value: str) -> str:
    if value not in OUTPUT_MODES:
        raise ValueError(f"output_mode must be one of {sorted(OUTPUT_MODES)}, got {value!r}")
    return value


def _default_trigger() -> dict[str, Any]:
    return {"enabled": False, "filters": {}}


class SqlAlchemyResourceBindingRepository:
    """SQLAlchemy-backed :class:`ResourceBindingRepository`.

    Constructor-injected with one :class:`AsyncSession`. The session owns
    the transaction; the repository never calls ``commit`` and never opens
    a new transaction. Mutation methods flush so the in-memory row carries
    the DB-assigned ids by the time the caller proceeds.
    """

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
        without shipping the others. A dict knob value is REPLACED, not
        merged (the caller decided the new shape).
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
        """Hard-delete the binding. Returns ``False`` if not present (or other workspace)."""
        row = await self.get(workspace_id=workspace_id, binding_id=binding_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def find_binding(
        self, *, connector_account_id: uuid.UUID, resource_id: str
    ) -> ResourceBindingRow | None:
        """Receive-stage lookup — resolve ``(account, resource)`` to a binding.

        Returns ``None`` on a miss (the inbound path turns that into "no
        product bound for this resource — skip" without raising). Does NOT
        take ``workspace_id`` because the inbound trail already resolved
        the account to a workspace upstream.
        """
        stmt = select(ResourceBindingRow).where(
            ResourceBindingRow.connector_account_id == connector_account_id,
            ResourceBindingRow.resource_id == resource_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


__all__ = ["SqlAlchemyResourceBindingRepository"]
