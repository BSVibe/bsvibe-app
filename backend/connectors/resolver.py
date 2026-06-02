"""Connector inbound resolution + dispatch (Workflow §11.2).

Resolves an external webhook delivery to a workspace and runs the matching
built-in connector parser:

1. Look up the active :class:`ConnectorAccountRow` by
   ``(connector, webhook_token)``. Missing / inactive → ``None`` (the HTTP
   route turns this into a 404 without leaking which half failed).
2. Decrypt the per-account signing secret.
3. Call the connector's pure parser, sourced from the engine's
   :class:`WebhookParserRegistry` (Lift Q3 / R2c) which the plugin loader
   populates from ``@bsvibe_sdk.webhook(...)``-decorated functions in each
   plugin. The parser verifies the signature (raising
   :class:`bsvibe_sdk.WebhookSignatureError` on a forged delivery) and
   returns a :class:`TriggerEvent`, or ``None`` to skip
   (handshake / unsupported).

Lift Q3 / R2c — this module used to ``from plugin.<name>.webhook import
parse_<x>`` directly, a reverse-direction coupling (backend → plugin). The
fix routes every parser through the engine's :class:`WebhookParserRegistry`
so the resolver depends only on the engine surface; plugins register their
parsers via the SDK ``@webhook(connector)`` decorator at load time.

Handshake answers (Slack ``url_verification`` challenge echo, Discord PING
PONG) are the route's responsibility — see
:func:`backend.connectors.handshake.handshake_response`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.webhook_registry import (
    ConnectorParser,
    WebhookParserRegistry,
)
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.domain.incoming import TriggerEvent

logger = structlog.get_logger(__name__)


class UnknownConnectorError(ValueError):
    """Raised when ``connector`` is not a known built-in (no parser)."""


@dataclass(slots=True)
class ConnectorDispatchResult:
    """Outcome of resolving + parsing one inbound connector delivery."""

    workspace_id: uuid.UUID
    connector: str
    event: TriggerEvent | None  # None ⇒ parser skipped (handshake / unsupported)


class ConnectorInboundResolver:
    """Resolve + dispatch one external connector webhook delivery."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        cipher: CredentialCipher,
        parsers: WebhookParserRegistry,
    ) -> None:
        self._session = session
        self._cipher = cipher
        self._parsers = parsers

    def is_known(self, connector: str) -> bool:
        return self._parsers.is_known(connector)

    async def resolve_account(
        self, *, connector: str, webhook_token: str
    ) -> ConnectorAccountRow | None:
        """Return the active account for ``(connector, webhook_token)`` or None.

        A missing row, a connector mismatch, or an inactive account all
        return ``None`` — the route maps every one to a 404 so a caller
        cannot tell which half of the pair was wrong.
        """
        stmt = select(ConnectorAccountRow).where(
            ConnectorAccountRow.connector == connector,
            ConnectorAccountRow.webhook_token == webhook_token,
            ConnectorAccountRow.is_active.is_(True),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def dispatch(
        self,
        *,
        connector: str,
        account: ConnectorAccountRow,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> ConnectorDispatchResult:
        """Decrypt the secret + run the connector parser for this delivery.

        Raises :class:`UnknownConnectorError` for an unmapped connector and
        propagates the parser's ``WebhookSignatureError`` on a forged
        delivery (the route turns that into a 401).
        """
        parser: ConnectorParser | None = self._parsers.get(connector)
        if parser is None:
            raise UnknownConnectorError(connector)

        secret = self._cipher.decrypt(account.signing_secret_ciphertext)
        event = parser(
            workspace_id=account.workspace_id,
            headers=headers,
            raw_body=raw_body,
            secret=secret,
        )
        logger.info(
            "connector_inbound_dispatched",
            connector=connector,
            workspace_id=str(account.workspace_id),
            skipped=event is None,
        )
        return ConnectorDispatchResult(
            workspace_id=account.workspace_id, connector=connector, event=event
        )


__all__ = [
    "ConnectorDispatchResult",
    "ConnectorInboundResolver",
    "ConnectorParser",
    "UnknownConnectorError",
]
