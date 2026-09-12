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

import uuid
from typing import TYPE_CHECKING, Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Path, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session
from backend.config import get_settings
from backend.connectors.handshake import handshake_response
from backend.connectors.interactions import interaction_callback
from backend.connectors.resolver import ConnectorInboundResolver, UnknownConnectorError
from backend.extensions.plugin.webhook_registry import (
    WebhookParserRegistry,
    get_default_registry,
)
from backend.identity.workspaces_db import ProductRow
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.shared.wire_kinds import (
    PAYLOAD_KEY_CONNECTOR_ACCOUNT_ID,
    PAYLOAD_KEY_RESOURCE_ID,
)
from backend.workers.emit import STREAM_INTAKE, emit_stream_notification, get_emit_redis_client
from backend.workflow.application.intake.webhook import WebhookReceiver
from bsvibe_sdk import WebhookSignatureError

if TYPE_CHECKING:  # pragma: no cover — annotation only, so this module keeps the
    # narrow runtime import surface R2c guards (see the contract in pyproject).
    from backend.connectors.db import ConnectorAccountRow
    from backend.identity.workspaces_db import ResourceBindingRow

logger = structlog.get_logger(__name__)

router = APIRouter()


# A connector's interactive-approval route entrypoint: settle a held Safe-Mode
# item from an inline tap. Returns ``True`` when it handled the tap (the route
# replies its default callback 200), ``False`` to fall through to the
# handshake/skip path, or its OWN :class:`Response` when the connector must reply
# a connector-specific body (Discord returns a DEFERRED ``{"type": 6}`` response
# carrying the background approval task — see ``process_discord_callback``).
def _repo_slug(repo: str) -> str:
    """Normalize a repo URL or ``owner/name`` to a lowercase ``owner/name`` so a
    connector's ``https://github.com/o/r`` matches a product's ``o/r`` binding."""
    s = repo.strip().lower().removesuffix(".git")
    parts = [
        p
        for p in s.replace("https://", "")
        .replace("http://", "")
        .replace("git@", "")
        .replace(":", "/")
        .split("/")
        if p
    ]
    return "/".join(parts[-2:]) if len(parts) >= 2 else s


#: Where each connector's RESOURCE identifier lives in its parsed payload.
#:
#: ``resource_bindings.resource_id`` is documented as *"Connector-shaped opaque
#: identifier … Free string — each connector defines its own grammar"*, and these
#: are the grammars the parsers actually emit (measured 2026-09-11):
#: telegram ``chat_id`` · discord ``channel_id`` · slack ``channel`` ·
#: github ``repo``.
#:
#: Declared here rather than inferred because a WRONG guess binds a founder's
#: message to the wrong product. A connector absent from this map resolves to
#: "no product" — loudly, via the guard in
#: ``tests/api/test_webhook_product_from_binding.py``, not silently in a chat.
_RESOURCE_ID_KEYS: dict[str, str] = {
    "telegram": "chat_id",
    "discord": "channel_id",
    "slack": "channel",
    "github": "repo",
    # A Sentry PROJECT is the unit a founder would bind to a product — the same
    # granularity as a github repo or a chat channel. (Added because the guard
    # test caught it missing: sentry can receive a webhook, so a founder could
    # bind it and get silence.)
    "sentry": "project",
}


def _resource_id_for(connector: str, payload: dict[str, Any]) -> str | None:
    """This event's connector-side resource id, or ``None``.

    Stringified because the wire types differ from what a founder types into a
    binding: telegram sends ``chat_id`` as a JSON NUMBER while the binding holds
    ``"8242700007"``. Comparing the raw types would never match — and would fail
    the way this whole class of bug fails, by quietly resolving to no product.
    """
    key = _RESOURCE_ID_KEYS.get(connector)
    if key is None:
        return None
    value = payload.get(key)
    if value is None or value == "":
        return None
    return str(value)


async def _binding_for_event(
    session: AsyncSession,
    *,
    account: ConnectorAccountRow,
    payload: dict[str, Any],
) -> tuple[ResourceBindingRow, str] | None:
    """The binding the founder made for THIS connector account + resource, plus
    the resource id it matched on.

    The call site the ``(connector_account_id, resource_id)`` index was built for
    — its own docstring says *"what Receive (B10b) will use to resolve an inbound
    webhook → binding → Product"* — and which had no caller until now. Prod
    already held the data (``BStockReport × telegram`` keyed on the founder's
    chat id) and nothing read it, so every chat message opened a run with no
    product: no repo to clone, "running unbound in an empty workspace".

    Keyed on the ACCOUNT the webhook token already resolved, so two workspaces
    binding the same channel string cannot reach each other's product.

    A miss is the normal state (a fresh connector, or a chat nobody bound) and
    yields ``None`` — never a guess. Picking "the workspace's only product" would
    work today and mis-route the moment there are two, which is exactly how this
    gap stayed invisible while there was one.
    """
    from backend.identity.infrastructure.repositories.resource_binding_repository_sql import (  # noqa: PLC0415, E501
        SqlAlchemyResourceBindingRepository,
    )

    resource_id = _resource_id_for(account.connector, payload)
    if resource_id is None:
        return None
    binding = await SqlAlchemyResourceBindingRepository(session).find_binding(
        connector_account_id=account.id, resource_id=resource_id
    )
    if binding is None:
        return None
    logger.info(
        "inbound_product_resolved_from_binding",
        connector=account.connector,
        product_id=str(binding.product_id),
        binding_id=str(binding.id),
    )
    return binding, resource_id


async def _resolve_inbound_product(
    session: AsyncSession,
    *,
    account: ConnectorAccountRow,
    parsed_product_id: uuid.UUID | None,
    payload: dict[str, Any],
) -> uuid.UUID | None:
    """Which product this delivery belongs to — and the Receive routing keys.

    Order is the point. The parser's own ``product_id`` wins if it set one; then
    the founder's EXPLICIT Product × Connector binding, because that is a stated
    intent where the repo path below is only an inference; then the repo match.
    If the fallback ran first, a github event whose repo matches one product
    would silently outrank a binding the founder made to a different one.

    **Side effect, and the reason this function exists.** When the binding
    resolves, ``payload`` is stamped in place with the ``(connector_account_id,
    resource_id)`` pair the Receive stage
    (:func:`backend.workflow.application.stages.intake.receive`) looks a binding
    up by — so the stage lands on the SAME binding off the durable row and
    applies its ``trigger.filters`` and ``selection`` enrichment. #922 resolved
    the binding here but never wrote those keys: prod's 13 webhook trigger
    events carried neither, Receive fell through to pass-through every single
    time, and ``filters`` went with it — never once applied in production.

    Only on an actual match. An unbound delivery keeps the payload the parser
    built, and with it today's pass-through: a stamped key with no binding
    behind it would only buy Receive a lookup that cannot hit.
    """
    if parsed_product_id is not None:
        return parsed_product_id

    resolved = await _binding_for_event(session, account=account, payload=payload)
    if resolved is not None:
        binding, resource_id = resolved
        payload[PAYLOAD_KEY_CONNECTOR_ACCOUNT_ID] = str(account.id)
        payload[PAYLOAD_KEY_RESOURCE_ID] = resource_id
        return binding.product_id

    # Unify inbound with the Direct path: a github issue/PR is processed like a
    # direct message ON the product it came from, so the run clones that repo +
    # works in context + delivers a repo-native PR — instead of running unbound
    # in an empty workspace.
    repo = payload.get("repo") or account.external_ref
    if repo:
        return await _product_id_for_repo(session, account.workspace_id, str(repo))
    return None


async def _product_id_for_repo(
    session: AsyncSession, workspace_id: uuid.UUID, repo: str
) -> uuid.UUID | None:
    """The workspace product bound to ``repo`` (matched on ``repo_url``), or
    ``None`` when none carries it — so the run binds to the issue's OWN repo and
    never to an unrelated one. This is what makes a github issue process like a
    Direct message ON that product (clone + work in context + repo-native PR)."""
    slug = _repo_slug(repo)
    if not slug:
        return None
    rows = (
        (await session.execute(select(ProductRow).where(ProductRow.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    for row in rows:
        if row.repo_url and _repo_slug(row.repo_url) == slug:
            return row.id
    return None


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
async def receive_connector_webhook(  # noqa: PLR0911 — 404/401/handshake/callback/skip/accept branches
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

    # Signature verified but no TriggerEvent: either a telegram inline-button
    # (callback_query) approve tap, a handshake that needs a specific body
    # (Slack url_verification / Discord PING), or a benign skip.
    if event is None:
        # An interactive approve/reject tap (telegram callback_query today; slack /
        # discord next) is a SYNCHRONOUS action, NOT a new run — it stays OUT of
        # intake. The connector signature was already verified by
        # ``resolver.dispatch`` above (the parser verifies then yields event=None
        # for an interaction). Dispatched by connector via a lazy import so this
        # module keeps zero plugin edges (R2c).
        callback = interaction_callback(connector)
        if callback is not None:
            handled = await callback(
                raw_body=raw_body, account=account, session=session, cipher=cipher
            )
            # A connector may reply with its OWN response (+ background task) instead
            # of the default callback 200 — e.g. Discord's DEFERRED ``{"type": 6}``
            # with the slow approval scheduled on a background task (its own fresh DB
            # session), so we do NOT commit the request session here.
            if isinstance(handled, Response):
                return handled
            if handled:
                await session.commit()
                return JSONResponse(
                    status_code=status.HTTP_200_OK,
                    content={"accepted": True, "callback": True},
                )
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
    # The payload we STORE — ``_resolve_inbound_product`` stamps the Receive
    # stage's routing keys onto it when the founder's binding matches.
    payload: dict[str, Any] = dict(event.payload or {})
    product_id = await _resolve_inbound_product(
        session, account=account, parsed_product_id=event.product_id, payload=payload
    )

    receiver = WebhookReceiver(session)
    outcome = await receiver.handle(
        workspace_id=event.workspace_id,
        source=event.source,
        headers={"X-Idempotency-Key": event.idempotency_key},
        body=payload,
        product_id=product_id,
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
