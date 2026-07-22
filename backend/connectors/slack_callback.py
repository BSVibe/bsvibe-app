"""Slack adapter for the connector-agnostic interactive-approval handler.

A founder taps 승인 / 거절 on the "작업 완료" (shipped) Block Kit card; this settles the
held Safe-Mode item straight from Slack — no PWA round trip. The connector-NEUTRAL
flow (resolve item, approve/deny + dispatch, audit actor, keep-body edit,
idempotency, best-effort ack) lives in :mod:`backend.connectors.approval_callback`;
this module is the thin SLACK ADAPTER — the slack-specific authorized-user gate,
the ``block_actions`` body predicate, the ``bot_token`` credential key + ``@p.action``
names, and the kwargs-builders for the ``response_url`` ephemeral ack /
``chat.update`` keep-body edit.

Boundary discipline (Lift Q3 / R2c): the route + this module never import
``plugin.slack``. ALL slack-specific work — parsing the interaction and the
Web-API calls — lives in the slack plugin and is reached through
:class:`PluginRunner` + :class:`PluginMeta` (the sanctioned backend→plugin
dispatch seam). The plugin is loaded at call time via importlib (see
:func:`_load_slack_meta`) so no static ``plugin.slack`` edge is introduced, which
keeps ``backend.api.webhooks → backend.connectors.slack_callback`` free of any
transitive ``plugin`` import (the R2c inbound-layer contract).

Security (do the auth BEFORE any state change): Slack delivers a card to a
CHANNEL, so any member can click. A tap is honoured ONLY when the tapper's
``user.id`` is on the account's ``delivery_config['authorized_user_ids']``
allowlist AND (when a ``team_id`` is bound) the tap's ``team.id`` matches it. An
empty / missing allowlist is FAIL-CLOSED (approval is irreversible) — see
:func:`_is_authorized_user`.
"""

from __future__ import annotations

import urllib.parse
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.approval_callback import (
    ApprovalConnectorAdapter,
    handle_approval_callback,
)
from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.base import PluginMeta
from backend.extensions.plugin.runner import PluginRunner
from backend.router.accounts.crypto import CredentialCipher


def _is_block_actions(body: dict[str, Any]) -> bool:
    """The inbound body is an approve/reject tap iff it is a ``block_actions``
    interaction payload (``process_slack_callback`` has already form-decoded it)."""
    return body.get("type") == "block_actions"


def _is_authorized_user(parsed: dict[str, Any], account: ConnectorAccountRow) -> bool:
    """The tap is an authorized user iff their ``user_id`` is on the account's
    ``authorized_user_ids`` allowlist AND — when a ``team_id`` is bound — the tap's
    ``team_id`` matches it.

    FAIL-CLOSED: an empty or missing allowlist authorizes NOBODY (approval is
    irreversible, and a channel card is tappable by any member). This is the
    human-authz layer; the transport signature was already verified upstream."""
    allowed = account.delivery_config.get("authorized_user_ids")
    if not isinstance(allowed, (list, tuple)) or not allowed:
        return False
    user_id = parsed.get("user_id")
    if user_id is None or str(user_id) not in {str(u) for u in allowed}:
        return False
    bound_team = account.delivery_config.get("team_id")
    if bound_team is not None and str(parsed.get("team_id")) != str(bound_team):
        return False
    return True


def _build_ack(parsed: dict[str, Any], text: str) -> dict[str, Any] | None:
    """``respond_ephemeral`` kwargs — an EPHEMERAL note to the tapper via the
    interactivity ``response_url``.

    Slack has no separate spinner-ack (HTTP 200 to the POST is the ack), so this is
    how the shared handler's ack text surfaces: an unauthorized tapper is told
    "권한이 없어요" and the acting founder gets a private confirmation, without touching
    the shared card (the card edit — :func:`_build_update` — is the public record).
    ``None`` when the payload carries no ``response_url``."""
    response_url = parsed.get("response_url")
    if not response_url:  # pragma: no cover - always present on a real tap
        return None
    return {"response_url": response_url, "text": text}


def _build_update(parsed: dict[str, Any], status: str) -> dict[str, Any] | None:
    """``chat.update`` kwargs that make the card read like HISTORY: rebuild the
    message blocks as the ORIGINAL blocks MINUS the ``actions`` (buttons) block,
    PLUS a ``context`` block carrying the localized ``status`` line. This KEEPS the
    card body (every non-button block, so the ``<url|보고서 보기>`` link survives) and
    drops the buttons.

    ``text`` is re-sent as the accessibility / notification fallback. Missing
    channel / message ts → ``None`` (nothing to edit)."""
    channel = parsed.get("channel_id")
    ts = parsed.get("message_ts")
    if not channel or not ts:  # pragma: no cover - a real card carries both
        return None
    original = parsed.get("message_blocks") or []
    blocks: list[dict[str, Any]] = [
        b for b in original if isinstance(b, dict) and b.get("type") != "actions"
    ]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": status}]})
    return {"channel": channel, "ts": ts, "text": status, "blocks": blocks}


# The slack surface the connector-agnostic handler plugs into.
SLACK_ADAPTER = ApprovalConnectorAdapter(
    connector="slack",
    credential_key="bot_token",
    parse_action="parse_slack_interaction",
    ack_action="respond_ephemeral",
    update_action="update_message",
    is_interaction=_is_block_actions,
    is_authorized=_is_authorized_user,
    build_ack=_build_ack,
    build_update=_build_update,
)


async def handle_slack_callback(
    *,
    raw_body: bytes,
    account: ConnectorAccountRow,
    session: AsyncSession,
    slack: PluginMeta,
    cipher: CredentialCipher,
    dispatcher: Any | None = None,
    runner: PluginRunner | None = None,
) -> bool:
    """Handle one slack ``block_actions`` approve/reject tap by delegating to the
    connector-agnostic :func:`handle_approval_callback` with the slack adapter.

    ``raw_body`` is the block_actions payload JSON (already form-decoded by
    :func:`process_slack_callback`). Returns ``True`` when the body was a
    block_actions interaction and was handled (the route replies 200), ``False``
    when it was not (the route falls through)."""
    return await handle_approval_callback(
        adapter=SLACK_ADAPTER,
        raw_body=raw_body,
        account=account,
        session=session,
        plugin=slack,
        cipher=cipher,
        dispatcher=dispatcher,
        runner=runner,
    )


@lru_cache(maxsize=1)
def _load_slack_meta() -> PluginMeta | None:
    """The loaded slack :class:`PluginMeta` (cached). ``None`` if the plugin is
    absent. Loads via importlib at call time — no static ``plugin.slack`` edge (so
    ``backend.api.webhooks`` stays free of the reverse coupling)."""
    from backend.extensions.plugin.loader import PluginLoader  # noqa: PLC0415

    plugins_dir = Path(__file__).resolve().parents[2] / "plugin"
    registry = PluginLoader(plugins_dir).load_all_sync()
    return registry.get("slack")


def _decode_form_payload(raw_body: bytes) -> bytes | None:
    """Extract the ``payload=<url-encoded JSON>`` field from a slack interactivity
    POST body and return the inner JSON as bytes (ready for the shared handler's
    ``json.loads``), or ``None`` when the body carries no ``payload`` field.

    Slack POSTs interactions form-encoded, but the connector-agnostic handler
    expects a JSON body; decoding here (generic ``parse_qs`` — no ``plugin.slack``
    import, keeping R2c clean) lets the shared handler stay UNCHANGED."""
    try:
        text = raw_body.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return None
    payloads = urllib.parse.parse_qs(text).get("payload")
    if not payloads:
        return None
    return payloads[0].encode("utf-8")


async def process_slack_callback(
    *,
    raw_body: bytes,
    account: ConnectorAccountRow,
    session: AsyncSession,
    cipher: CredentialCipher,
    dispatcher: Any | None = None,
) -> bool:
    """Route-facing entrypoint: form-decode the interactivity body, load the slack
    plugin + delegate to :func:`handle_slack_callback` with the production
    dispatcher. Returns ``False`` (route falls through) when the body is not a
    form-encoded interactivity POST or the slack plugin is unavailable."""
    payload_json = _decode_form_payload(raw_body)
    if payload_json is None:
        return False
    slack = _load_slack_meta()
    if slack is None:  # pragma: no cover - slack is always loaded in prod
        return False
    return await handle_slack_callback(
        raw_body=payload_json,
        account=account,
        session=session,
        slack=slack,
        cipher=cipher,
        dispatcher=dispatcher,
    )


__all__ = [
    "SLACK_ADAPTER",
    "handle_slack_callback",
    "process_slack_callback",
]
