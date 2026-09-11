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
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    product_channel_ids: set[uuid.UUID] | None = None,
) -> list[NotifyBinding]:
    """Active connector_accounts for the workspace that are notify channels.

    Selection only — no credential decrypt, no plugin invoke (that is N2). Rows
    failing any qualifying condition (see module docstring) are skipped; a
    connector without a notify builder is the deliberate seam.

    ``product_channel_ids`` narrows to the channels the founder EXPLICITLY bound
    to the product this notification is about — the same per-product axis the
    inbound path resolves on. Without it every product's notifications went to
    every channel, and the card did not even name which product it was about.

    The ids are RESOLVED BY THE CALLER, not queried here: ``resource_bindings``
    lives in ``backend.identity``, and this module is a common leaf that may not
    depend on a bounded context. Passing the set in keeps the leaf rule intact
    and keeps the selection policy (below) in one place.

    **The narrowing FALLS BACK deliberately.** A product with no *notify* binding
    keeps the workspace's channels. Measured in prod: ``BSVibe`` is bound only to
    github, which is not a notify channel — a straight filter would give it ZERO
    channels and silently stop alerts the founder receives today. Losing an alert
    is strictly worse than one arriving on a shared channel, so the filter only
    ever narrows when there is something to narrow TO.
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
    return _narrow_to_product(bindings, product_channel_ids)


def _narrow_to_product(
    bindings: list[NotifyBinding], bound_account_ids: set[uuid.UUID] | None
) -> list[NotifyBinding]:
    """Keep only the product's bound channels — unless that would keep none.

    ``None`` means no product was given (a workspace-level notification), which
    is not the same as "a product that bound nothing": both fall back, but for
    different reasons, and neither may end up silent.
    """
    if bound_account_ids is None:
        return bindings
    narrowed = [b for b in bindings if b.account.id in bound_account_ids]
    return narrowed or bindings


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
