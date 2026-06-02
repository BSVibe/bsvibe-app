"""Public connector webhook ingress — ``POST /api/webhooks/{connector}/{token}``.

The connector-inbound entrypoint (Workflow §11.2). An external provider
(github / slack / telegram / discord / sentry) POSTs a signed delivery here;
we resolve the ``(connector, webhook_token)`` pair to a workspace, verify
the signature via the registered plugin parser, and land a
``TriggerEvent(source=<connector>, trigger_kind=webhook)`` on the EXISTING
intake path (:class:`backend.workflow.application.intake.webhook.WebhookReceiver`). From there the
IntakeWorker → Request → ... → Safe Mode delivery pipeline already wired
(PR #17) drives it; because ``workspace.safe_mode`` defaults True, connector
deliveries queue for founder approval — exactly the §11.2 intent.

Lift Q3 / R2c — this route used to import each plugin's local
``WebhookSignatureError`` subclass directly (``from plugin.github.webhook
import WebhookSignatureError as GithubSignatureError`` ...). After Lift
Q3 every plugin's local subclass extends the SDK base
:class:`bsvibe_sdk.WebhookSignatureError`, and parsers are dispatched via
the engine's :class:`WebhookParserRegistry`; a single
``except bsvibe_sdk.WebhookSignatureError`` here catches every connector's
forgery. The reverse-direction imports from ``plugin.*.webhook`` are gone.

This route is **PUBLIC** (no founder auth): it is an external callback. The
``webhook_token`` is the unguessable capability (``secrets.token_urlsafe(32)``)
*and* the per-connector signature on the body is verified — those two together
are the auth. It is mounted under ``/api`` directly, NOT under the authed v1
router.

Response contract:
* 404 — no active account for ``(connector, webhook_token)`` (does not leak
  which half failed); also unknown connector.
* 401 — signature verification failed (forged delivery).
* 200 — handshake (Slack ``url_verification`` challenge echo / Discord PING
  PONG); body is the handshake reply.
* 202 — accepted (a TriggerEvent landed, or the delivery was a benign skip
  such as an unsupported event type / bot author).
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Path, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session
from backend.config import get_settings
from backend.connectors.handshake import handshake_response
from backend.connectors.resolver import ConnectorInboundResolver, UnknownConnectorError
from backend.extensions.plugin.webhook_registry import (
    WebhookParserRegistry,
    get_default_registry,
)
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.workers.emit import STREAM_INTAKE, emit_stream_notification, get_emit_redis_client
from backend.workflow.application.intake.webhook import WebhookReceiver
from bsvibe_sdk import WebhookSignatureError

logger = structlog.get_logger(__name__)

router = APIRouter()


def get_credential_cipher() -> CredentialCipher:
    """Build the credential cipher from settings (test-overridable)."""
    return CredentialCipher(_key_from_settings())


def get_webhook_parser_registry() -> WebhookParserRegistry:
    """Engine-side parser registry dependency (test-overridable).

    Defaults to the process-wide singleton the plugin loader populates at
    bootstrap. Tests inject a tailored :class:`WebhookParserRegistry`
    instance via FastAPI's ``dependency_overrides`` to drive specific
    connector / parser combinations without touching the singleton.
    """
    return get_default_registry()


@router.post("/webhooks/{connector}/{webhook_token}")
async def receive_connector_webhook(
    request: Request,
    connector: Annotated[str, Path(max_length=64)],
    webhook_token: Annotated[str, Path(max_length=128)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
    parsers: Annotated[WebhookParserRegistry, Depends(get_webhook_parser_registry)],
) -> Any:
    """Ingest one external signed connector webhook delivery (PUBLIC)."""
    resolver = ConnectorInboundResolver(session, cipher=cipher, parsers=parsers)

    # Unknown connector OR no active account for the (connector, token) pair →
    # one opaque 404 (do not leak which half failed).
    account = (
        await resolver.resolve_account(connector=connector, webhook_token=webhook_token)
        if resolver.is_known(connector)
        else None
    )
    if account is None:
        return _not_found()

    raw_body = await request.body()
    headers = dict(request.headers)

    try:
        result = await resolver.dispatch(
            connector=connector,
            account=account,
            headers=headers,
            raw_body=raw_body,
        )
    except WebhookSignatureError:
        logger.info(
            "connector_inbound_signature_rejected",
            connector=connector,
            workspace_id=str(account.workspace_id),
        )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "signature verification failed"},
        )
    except UnknownConnectorError:  # pragma: no cover - guarded by is_known above
        return _not_found()

    event = result.event

    # Signature verified but no TriggerEvent: either a handshake that needs a
    # specific body (Slack url_verification / Discord PING) or a benign skip.
    if event is None:
        reply = handshake_response(connector, raw_body)
        if reply is not None:
            return JSONResponse(status_code=status.HTTP_200_OK, content=reply)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"accepted": True, "skipped": True},
        )

    # Valid TriggerEvent → persist via the existing intake receiver (idempotent
    # on (workspace_id, source, idempotency_key)). source = the connector name.
    # The parser already computed a stable idempotency_key (e.g. Slack event_id,
    # GitHub delivery id); thread it through the header the receiver honours so
    # a redelivery collapses regardless of header presence on the wire.
    receiver = WebhookReceiver(session)
    outcome = await receiver.handle(
        workspace_id=event.workspace_id,
        source=event.source,
        headers={"X-Idempotency-Key": event.idempotency_key},
        body=event.payload,
        product_id=event.product_id,
        trace_id=event.trace_id,
    )
    await session.commit()

    # AFTER the TriggerEvent is durable, wake the IntakeWorker consumer on the
    # ``intake`` stream (same gated + soft-fail contract as the Direct path). A
    # redelivery that collapsed (duplicate) landed no new row → no emit. In
    # db_polling (default) no Redis client is built and this is a pure no-op.
    if not outcome.duplicate:
        settings = get_settings()
        await emit_stream_notification(
            get_emit_redis_client(settings),
            settings=settings,
            stream=STREAM_INTAKE,
            fields={"workspace_id": str(event.workspace_id)},
        )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"accepted": True, "duplicate": outcome.duplicate},
    )


def _not_found() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": "unknown connector webhook"},
    )


__all__ = [
    "get_credential_cipher",
    "get_webhook_parser_registry",
    "receive_connector_webhook",
    "router",
]
