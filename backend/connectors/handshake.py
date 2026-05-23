"""Connector handshake answers (Workflow §11.2).

Some connectors require a synchronous handshake reply that is NOT a
TriggerEvent and must not enter the workflow:

* **Slack** ``url_verification`` — Slack POSTs ``{"type":"url_verification",
  "challenge": "<token>"}`` once when you save the Request URL; the endpoint
  must echo ``{"challenge": "<token>"}`` (HTTP 200).
* **Discord** PING (``{"type":1}``) — Discord requires every interactions
  endpoint to answer the registration PING with ``{"type":1}`` PONG (HTTP
  200), after Ed25519 verification.

The connector parsers already return ``None`` for both cases (and have
already verified the signature by the time we get here), so the route runs
verify+parse first, then asks this helper whether the (skipped) delivery is
a handshake that still needs a specific body.
"""

from __future__ import annotations

import json
from typing import Any

DISCORD_PING = 1
DISCORD_PONG: dict[str, Any] = {"type": 1}


def handshake_response(connector: str, raw_body: bytes) -> dict[str, Any] | None:
    """Return the JSON body for a handshake delivery, or ``None`` if not one.

    Called only after signature verification has passed (the parser ran and
    returned ``None``). A malformed body is treated as "not a handshake"
    (``None``) — the route then returns its normal accepted-but-skipped 202.
    """
    try:
        body: dict[str, Any] = json.loads(raw_body)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None

    if connector == "slack" and body.get("type") == "url_verification":
        challenge = body.get("challenge")
        if isinstance(challenge, str):
            return {"challenge": challenge}
        return None

    if connector == "discord" and body.get("type") == DISCORD_PING:
        return dict(DISCORD_PONG)

    return None


__all__ = ["DISCORD_PING", "DISCORD_PONG", "handshake_response"]
