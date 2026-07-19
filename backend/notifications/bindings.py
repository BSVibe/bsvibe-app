"""Workspace → notification-channel binding resolution (Notifier N1a).

The notification channel model is DERIVED from connectors, not hardcoded. A
workspace's channels are ``["in_app"]`` plus every connector it has bound that
is a notify channel — mirroring how outbound delivery derives its targets from
``connector_accounts`` (:func:`...connector_dispatch._resolver._resolve_bindings`).

This is the SELECTION half of that resolver, cloned for notify: it picks the
qualifying ``connector_accounts`` rows but does NOT decrypt the credential or
invoke the plugin (that is N2's sending path). A row qualifies when ALL hold:

* it is ``is_active`` for the workspace,
* its ``delivery_config`` is non-empty (a configured target),
* its ``connector`` is in :data:`NOTIFY_EVENT_BUILDERS` (i.e. it is a notify
  channel, not a deliberate seam), AND
* the connector is ``user_connectable`` — i.e. not in
  :data:`backend.connectors.hidden.HIDDEN_CONNECTORS`. ``user_connectable`` is
  *defined* as "not hidden", so this reads the same source of truth the catalog's
  ``ConnectorInfo.user_connectable`` derives from, without importing the
  plugin-loader-backed catalog across the leaf import boundary. (Being in
  ``NOTIFY_EVENT_BUILDERS`` already implies a real plugin exists, so the
  catalog's "connector exists" check is subsumed by that membership.)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.db import ConnectorAccountRow
from backend.connectors.hidden import HIDDEN_CONNECTORS
from backend.notifications.notify_builders import NOTIFY_EVENT_BUILDERS

logger = structlog.get_logger(__name__)

# The always-present channel: the in-app inbox (live-events SSE + Brief "Needs
# you" + nav badge). It is not a connector, so it is prepended unconditionally.
IN_APP_CHANNEL = "in_app"


@dataclass(slots=True)
class NotifyBinding:
    """A workspace's active binding of one notify-channel connector."""

    account: ConnectorAccountRow
    connector: str


async def resolve_notify_bindings(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> list[NotifyBinding]:
    """Active connector_accounts for the workspace that are notify channels.

    Selection only — no credential decrypt, no plugin invoke (that is N2). Rows
    failing any qualifying condition (see module docstring) are skipped; a
    connector without a notify builder is the deliberate seam.
    """
    rows = (
        (
            await session.execute(
                select(ConnectorAccountRow).where(
                    ConnectorAccountRow.workspace_id == workspace_id,
                    ConnectorAccountRow.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    bindings: list[NotifyBinding] = []
    for row in rows:
        if not row.delivery_config:
            continue
        if row.connector not in NOTIFY_EVENT_BUILDERS:
            continue
        if row.connector in HIDDEN_CONNECTORS:
            logger.info(
                "notify_binding_hidden_connector_skipped",
                connector=row.connector,
                workspace_id=str(workspace_id),
            )
            continue
        bindings.append(NotifyBinding(account=row, connector=row.connector))
    return bindings


async def available_channels(session: AsyncSession, *, workspace_id: uuid.UUID) -> list[str]:
    """The workspace's notification channels: ``in_app`` + derived connectors.

    Always recomputed at read time (never a stored column) so a newly-bound
    connector appears as a channel with no migration.
    """
    bindings = await resolve_notify_bindings(session, workspace_id=workspace_id)
    connectors = sorted({b.connector for b in bindings})
    return [IN_APP_CHANNEL, *connectors]


__all__ = [
    "IN_APP_CHANNEL",
    "NotifyBinding",
    "available_channels",
    "resolve_notify_bindings",
]
