"""Connector inbound — external signed webhooks → TriggerEvent (Workflow §11.2).

A ``connector_account`` binds one external connector (github / slack /
telegram / discord) registered for a workspace to an unguessable
``webhook_token``. An external provider POSTs to
``/api/webhooks/{connector}/{webhook_token}``; the row resolves the
workspace + decrypts the per-account signing secret, the matching plugin
inbound parser verifies the signature, and a valid delivery lands a
``TriggerEvent(source=<connector>, trigger_kind=webhook)`` on the existing
intake path (which Safe Mode then queues for founder approval, PR #17).

This package owns the *ingress* surface only: the table, the
connector→workspace resolution, and the dispatch into the built-in
plugin parsers. The founder-facing CRUD to register a connector account
is a follow-up (not in this chunk).
"""

from __future__ import annotations

from backend.connectors.catalog import (
    ConnectorInfo,
    build_connector_catalog,
    get_connector_catalog,
)
from backend.connectors.db import ConnectorAccountRow
from backend.connectors.resolver import (
    ConnectorDispatchResult,
    ConnectorInboundResolver,
    UnknownConnectorError,
)

__all__ = [
    "ConnectorAccountRow",
    "ConnectorDispatchResult",
    "ConnectorInboundResolver",
    "ConnectorInfo",
    "UnknownConnectorError",
    "build_connector_catalog",
    "get_connector_catalog",
]
