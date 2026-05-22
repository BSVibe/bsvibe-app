"""Discord interaction webhook parsing → :class:`backend.intake.schema.TriggerEvent`.

Pure functions (no I/O) so they unit-test without httpx. The inbound capability
in :mod:`backend.plugins.implementations.discord.plugin` wires the configured
``public_key`` credential into :func:`parse_interaction`.

Security: Discord's webhook auth is the **Ed25519 request-signing** scheme. Each
delivery carries ``X-Signature-Ed25519`` (a hex-encoded signature) and
``X-Signature-Timestamp``. Verification checks::

    Ed25519(public_key).verify(signature, timestamp + raw_body)

using the application's Ed25519 public key (registered in the Discord developer
portal). The verification is via the ``cryptography`` library's Ed25519
primitives — already a project dependency (``cryptography>=42`` in
pyproject.toml), so no new dependency is added. ``Ed25519PublicKey.verify`` is
the constant-time primitive; it raises ``InvalidSignature`` on mismatch (which
we translate to :class:`WebhookSignatureError`).

The PING interaction (``type == 1``) still runs the verify path (Discord
requires every endpoint to verify even the registration PING), but parsing
returns ``None`` — the PING is answered with a PONG by the HTTP route (out of
this track's scope). Idempotency is keyed off the interaction ``id`` (a Discord
snowflake, unique per interaction), so a redelivery collapses to the same
intake row. Bot-authored messages are skipped to avoid self-trigger loops.
"""

from __future__ import annotations

import binascii
import json
import uuid
from typing import Any

import structlog
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from backend.intake.schema import TriggerEvent

logger = structlog.get_logger(__name__)

# Discord delivers the signature + timestamp in these headers on every
# interaction. (Header *names*, not secret values — S105 false positive.)
SIGNATURE_HEADER = "x-signature-ed25519"
TIMESTAMP_HEADER = "x-signature-timestamp"

# Discord interaction types (subset). PING is answered by the HTTP route.
INTERACTION_PING = 1
# Interaction types this connector turns into TriggerEvents. PING (1) is the
# registration handshake (skip → None); the rest are real user-driven
# interactions (slash commands, components, modal submits, autocomplete).
SUPPORTED_INTERACTION_TYPES = frozenset({2, 3, 4, 5})


class WebhookError(ValueError):
    """Raised when an interaction cannot be parsed (malformed / missing fields)."""


class WebhookSignatureError(WebhookError):
    """Raised when Ed25519 verification fails — treat as forged."""


def verify_signature(
    public_key_hex: str,
    raw_body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
) -> None:
    """Raise :class:`WebhookSignatureError` unless the Ed25519 signature matches.

    Implements Discord's request-signing scheme: the application's Ed25519
    public key (hex) verifies ``X-Signature-Ed25519`` (hex) over the bytes
    ``timestamp + raw_body``. ``Ed25519PublicKey.verify`` is the constant-time
    primitive and raises ``InvalidSignature`` on any mismatch.
    """
    if not signature_header:
        raise WebhookSignatureError("missing X-Signature-Ed25519 header")
    if not timestamp_header:
        raise WebhookSignatureError("missing X-Signature-Timestamp header")
    try:
        public_key_bytes = binascii.unhexlify(public_key_hex)
        signature = binascii.unhexlify(signature_header)
    except (binascii.Error, ValueError) as exc:
        raise WebhookSignatureError(f"invalid hex encoding: {exc}") from exc
    try:
        verifier = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    except ValueError as exc:
        raise WebhookSignatureError(f"invalid Ed25519 public key: {exc}") from exc
    message = timestamp_header.encode() + raw_body
    try:
        verifier.verify(signature, message)
    except InvalidSignature as exc:
        raise WebhookSignatureError("signature mismatch") from exc


def _lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def parse_interaction(
    *,
    workspace_id: uuid.UUID,
    headers: dict[str, str],
    raw_body: bytes,
    public_key: str | None = None,
) -> TriggerEvent | None:
    """Parse one Discord interaction webhook delivery.

    Returns a :class:`TriggerEvent` for supported interactions, or ``None`` to
    skip (the PING handshake, unsupported interaction types, bot authors).
    Raises :class:`WebhookSignatureError` when ``public_key`` is given and
    Ed25519 verification fails, :class:`WebhookError` on malformed input.

    The PING (``type=1``) still runs the verify path (Discord requires every
    endpoint to verify the registration PING) but returns ``None`` — the PONG
    is sent by the HTTP route, not the workflow.
    """
    h = _lower_headers(headers)

    if public_key is not None:
        verify_signature(
            public_key,
            raw_body,
            h.get(SIGNATURE_HEADER),
            h.get(TIMESTAMP_HEADER),
        )

    try:
        body: dict[str, Any] = json.loads(raw_body)
    except (ValueError, TypeError) as exc:
        raise WebhookError(f"discord interaction body is not valid JSON: {exc}") from exc

    interaction_type = body.get("type")
    # The registration PING is answered by the HTTP route with a PONG — skip it
    # here (it must still have passed verify_signature above).
    if interaction_type == INTERACTION_PING:
        return None
    if interaction_type not in SUPPORTED_INTERACTION_TYPES:
        logger.debug("discord_interaction_skip_type", interaction_type=interaction_type)
        return None

    interaction_id = body.get("id")
    if not interaction_id:
        raise WebhookError("missing interaction id")

    # Skip bot-authored interactions to avoid self-trigger loops. A user object
    # lives at body["member"]["user"] (guild context) or body["user"] (DM).
    member = body.get("member") or {}
    user = member.get("user") or body.get("user") or {}
    if user.get("bot"):
        return None

    data = body.get("data") or {}
    payload = {
        "interaction_type": interaction_type,
        "interaction_id": interaction_id,
        "channel_id": body.get("channel_id"),
        "guild_id": body.get("guild_id"),
        "user_id": user.get("id"),
        "command_name": data.get("name"),
        "body": body,
    }
    return TriggerEvent(
        workspace_id=workspace_id,
        source="discord",
        trigger_kind="webhook",
        idempotency_key=f"discord:{interaction_id}",
        payload=payload,
        trace_id=str(interaction_id),
    )


__all__ = [
    "INTERACTION_PING",
    "SIGNATURE_HEADER",
    "SUPPORTED_INTERACTION_TYPES",
    "TIMESTAMP_HEADER",
    "WebhookError",
    "WebhookSignatureError",
    "parse_interaction",
    "verify_signature",
]
