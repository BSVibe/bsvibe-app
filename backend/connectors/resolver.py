"""Connector inbound resolution + dispatch (Workflow §11.2).

Resolves an external webhook delivery to a workspace and runs the matching
built-in connector parser:

1. Look up the active :class:`ConnectorAccountRow` by
   ``(connector, webhook_token)``. Missing / inactive → ``None`` (the HTTP
   route turns this into a 404 without leaking which half failed).
2. Decrypt the per-account signing secret.
3. Call the connector's pure parser (``backend.plugins.implementations.*``)
   with the raw body + headers + secret. The parser verifies the signature
   (raising :class:`WebhookSignatureError` on a forged delivery) and returns
   a :class:`TriggerEvent`, or ``None`` to skip (handshake / unsupported).

Why a direct import map instead of :class:`backend.plugins.PluginLoader`:
the four built-in inbound parsers are pure ``(workspace_id, headers,
raw_body, secret) -> TriggerEvent | None`` functions with no I/O and no
``SkillContext``/credential-injection dependency. Dispatching to them
directly keeps the ingress surface small and side-effect-free — the loader
path (with its context + credential store) is the right tool for the agent
loop, not for a stateless signature-verify-and-parse hop.

Handshake answers (Slack ``url_verification`` challenge echo, Discord PING
PONG) are the route's responsibility — see
:func:`backend.connectors.handshake.handshake_response`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.accounts.crypto import CredentialCipher
from backend.connectors.db import ConnectorAccountRow
from backend.intake.schema import TriggerEvent
from backend.plugins.implementations.discord.webhook import parse_interaction
from backend.plugins.implementations.github.webhook import parse_webhook
from backend.plugins.implementations.slack.webhook import parse_event
from backend.plugins.implementations.telegram.webhook import parse_update

logger = structlog.get_logger(__name__)


# A connector parser: verify the signature with ``secret`` and map the raw
# delivery to a TriggerEvent (or None to skip). Each built-in parser already
# matches this shape; the discord parser's secret kwarg is the Ed25519
# ``public_key`` (semantically still the verifying secret), wrapped below.
ConnectorParser = Callable[..., TriggerEvent | None]


def _discord_parser(
    *, workspace_id: uuid.UUID, headers: dict[str, str], raw_body: bytes, secret: str | None
) -> TriggerEvent | None:
    # Discord's verifying material is the application's Ed25519 public key;
    # we store it in the same ``signing_secret`` slot for uniform resolution.
    return parse_interaction(
        workspace_id=workspace_id, headers=headers, raw_body=raw_body, public_key=secret
    )


# connector name → parser. The names match the plugin ``name=`` (Workflow §6)
# and the ``TriggerEvent.source`` each parser emits.
_PARSERS: dict[str, ConnectorParser] = {
    "github": parse_webhook,
    "slack": parse_event,
    "telegram": parse_update,
    "discord": _discord_parser,
}


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

    def __init__(self, session: AsyncSession, *, cipher: CredentialCipher) -> None:
        self._session = session
        self._cipher = cipher

    @staticmethod
    def is_known(connector: str) -> bool:
        return connector in _PARSERS

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
        parser = _PARSERS.get(connector)
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
