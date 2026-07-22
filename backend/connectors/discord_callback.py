"""Discord adapter for the connector-agnostic interactive-approval handler.

A founder taps 승인 / 거절 on the "작업 완료" (shipped) message-component card; this
settles the held Safe-Mode item straight from Discord — no PWA round trip. The
connector-NEUTRAL flow (resolve item, approve/deny + dispatch, audit actor,
keep-body edit, idempotency, best-effort ack) lives in
:mod:`backend.connectors.approval_callback`; this module is the thin DISCORD
ADAPTER — the discord-specific authorized-user gate, the component-interaction
body predicate, the ``bot_token`` credential key + ``@p.action`` names, and the
kwargs-builders for the interaction-webhook EPHEMERAL follow-up ack /
``@original`` keep-body edit.

THE TIMING MODEL (Discord-specific). Discord requires the interactions endpoint to
answer within ~3s, but our approve + ``dispatch_delivery`` (opens a GitHub PR) can
take longer. So :func:`process_discord_callback` returns a SYNCHRONOUS DEFERRED
interaction response (``{"type": 6}`` — DEFERRED_UPDATE_MESSAGE) and schedules the
slow approve/dispatch/edit as a Starlette :class:`BackgroundTask` that opens a
FRESH DB session (the request session is closed once the response is sent). The
generic handler then runs off that fresh session — never the request one.

Boundary discipline (Lift Q3 / R2c): the route + this module never import
``plugin.discord``. ALL discord-specific work — parsing the interaction and the
interaction-webhook calls — lives in the discord plugin and is reached through
:class:`PluginRunner` + :class:`PluginMeta` (the sanctioned backend→plugin dispatch
seam). The plugin is loaded at call time via importlib (see
:func:`_load_discord_meta`) so no static ``plugin.discord`` edge is introduced.

Security (do the auth BEFORE any state change): Discord delivers a card to a
CHANNEL, so any member can click. A tap is honoured ONLY when the tapper's
``user_id`` is on the account's ``delivery_config['authorized_user_ids']`` allowlist
AND (when a ``guild_id`` is bound) the tap's ``guild_id`` matches it. An empty /
missing allowlist is FAIL-CLOSED (approval is irreversible) — see
:func:`_is_authorized_user`. Auth runs inside the background task, but the
irreversible approve is likewise deferred, so nothing settles before the gate.

Ack / edit authentication: Discord follow-ups + the ``@original`` edit use the
INTERACTION WEBHOOK (``application_id`` + ``interaction_token`` from the payload,
valid ~15 min) — no bot token is actually needed. We still set
``credential_key="bot_token"`` (matching discord's outbound slot) so the shared
handler's context build has a valid credential to inject; the interaction-webhook
client methods just don't rely on it.
"""

from __future__ import annotations

import json
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.background import BackgroundTask

from backend.connectors.approval_callback import (
    ApprovalConnectorAdapter,
    handle_approval_callback,
)
from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.base import PluginMeta
from backend.extensions.plugin.runner import PluginRunner
from backend.router.accounts.crypto import CredentialCipher

# Discord interaction type-3 (MESSAGE_COMPONENT) — a button tap on an existing card.
INTERACTION_COMPONENT = 3
# The synchronous interaction response we return within Discord's ~3s window:
# DEFERRED_UPDATE_MESSAGE — "acknowledged; I'll edit the message shortly" (keeps the
# card unchanged until the background task edits @original). Discord's numeric type.
DEFERRED_UPDATE_MESSAGE = 6
# Ephemeral message flag (only the tapper sees the follow-up).
EPHEMERAL_FLAG = 64

# custom_id verbs an approve/reject tap can carry (mirror notify_builders).
_INTERACTION_VERBS = frozenset({"apv", "rej"})


def _is_component_interaction(body: dict[str, Any]) -> bool:
    """The inbound body is an approve/reject tap iff it is a message-component
    interaction (type 3) whose ``data.custom_id`` is an ``apv:``/``rej:`` verb."""
    if body.get("type") != INTERACTION_COMPONENT:
        return False
    data = body.get("data") or {}
    verb, _, _ = str(data.get("custom_id") or "").partition(":")
    return verb in _INTERACTION_VERBS


def _is_authorized_user(parsed: dict[str, Any], account: ConnectorAccountRow) -> bool:
    """The tap is an authorized user iff their ``user_id`` is on the account's
    ``authorized_user_ids`` allowlist AND — when a ``guild_id`` is bound — the tap's
    ``guild_id`` matches it (Discord guild = slack team).

    FAIL-CLOSED: an empty or missing allowlist authorizes NOBODY (approval is
    irreversible, and a channel card is tappable by any member). This is the
    human-authz layer; the Ed25519 transport signature was already verified
    upstream."""
    allowed = account.delivery_config.get("authorized_user_ids")
    if not isinstance(allowed, (list, tuple)) or not allowed:
        return False
    user_id = parsed.get("user_id")
    if user_id is None or str(user_id) not in {str(u) for u in allowed}:
        return False
    bound_guild = account.delivery_config.get("guild_id")
    if bound_guild is not None and str(parsed.get("guild_id")) != str(bound_guild):
        return False
    return True


def _build_ack(parsed: dict[str, Any], text: str) -> dict[str, Any] | None:
    """``discord_followup`` kwargs — an EPHEMERAL follow-up to the tapper via the
    interaction webhook (``application_id`` + ``interaction_token``).

    This is how the shared handler's ack text surfaces: an unauthorized tapper is
    told "권한이 없어요" and the acting founder gets a private confirmation, WITHOUT
    editing the shared card (the card edit — :func:`_build_update` — is the public
    record; the buttons stay for the real founder on an unauthorized tap). ``None``
    when the payload carries no application_id / interaction_token."""
    application_id = parsed.get("application_id")
    interaction_token = parsed.get("interaction_token")
    if not application_id or not interaction_token:  # pragma: no cover - always present on a tap
        return None
    return {
        "application_id": application_id,
        "interaction_token": interaction_token,
        "content": text,
        "flags": EPHEMERAL_FLAG,
    }


def _build_update(parsed: dict[str, Any], status: str) -> dict[str, Any] | None:
    """``discord_edit_original`` kwargs that make the card read like HISTORY: KEEP
    the original message content (incl. the ``[보고서 보기](url)`` markdown link),
    APPEND the approve/reject ``status`` line after it, and DROP the buttons
    (``components=[]``).

    Falls back to the ``status`` line alone when the interaction carries no original
    content (shouldn't happen for a real card). Missing application_id /
    interaction_token → ``None`` (nothing to edit)."""
    application_id = parsed.get("application_id")
    interaction_token = parsed.get("interaction_token")
    if not application_id or not interaction_token:  # pragma: no cover - always present on a tap
        return None
    original = parsed.get("message_content")
    content = f"{original}\n\n{status}" if original else status
    return {
        "application_id": application_id,
        "interaction_token": interaction_token,
        "content": content,
        "components": [],
    }


# The discord surface the connector-agnostic handler plugs into.
DISCORD_ADAPTER = ApprovalConnectorAdapter(
    connector="discord",
    credential_key="bot_token",
    parse_action="parse_discord_interaction",
    ack_action="discord_followup",
    update_action="discord_edit_original",
    is_interaction=_is_component_interaction,
    is_authorized=_is_authorized_user,
    build_ack=_build_ack,
    build_update=_build_update,
)


async def handle_discord_callback(
    *,
    raw_body: bytes,
    account: ConnectorAccountRow,
    session: AsyncSession,
    discord: PluginMeta,
    cipher: CredentialCipher,
    dispatcher: Any | None = None,
    runner: PluginRunner | None = None,
) -> bool:
    """Handle one discord component approve/reject tap by delegating to the
    connector-agnostic :func:`handle_approval_callback` with the discord adapter.

    ``session`` is the (fresh, background-task) session the state change runs on.
    Returns ``True`` when the body was a component interaction and was handled,
    ``False`` when it was not (a PING / non-component body)."""
    return await handle_approval_callback(
        adapter=DISCORD_ADAPTER,
        raw_body=raw_body,
        account=account,
        session=session,
        plugin=discord,
        cipher=cipher,
        dispatcher=dispatcher,
        runner=runner,
    )


@lru_cache(maxsize=1)
def _load_discord_meta() -> PluginMeta | None:
    """The loaded discord :class:`PluginMeta` (cached). ``None`` if the plugin is
    absent. Loads via importlib at call time — no static ``plugin.discord`` edge (so
    ``backend.api.webhooks`` stays free of the reverse coupling)."""
    from backend.extensions.plugin.loader import PluginLoader  # noqa: PLC0415

    plugins_dir = Path(__file__).resolve().parents[2] / "plugin"
    registry = PluginLoader(plugins_dir).load_all_sync()
    return registry.get("discord")


def _load_json(raw_body: bytes) -> Any:
    try:
        return json.loads(raw_body)
    except (ValueError, TypeError):
        return None


async def _run_discord_approval(
    *,
    account_id: uuid.UUID,
    raw_body: bytes,
    cipher: CredentialCipher,
    dispatcher: Any | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    """The deferred approval work — runs AFTER the type-6 response ships, on a FRESH
    DB session (the request session is closed by then). Re-loads the account by id
    from the fresh session, loads the discord plugin, and delegates to the shared
    handler. Best-effort: a missing account / plugin is a silent no-op."""
    if session_factory is None:
        from backend.api.deps import _get_session_factory  # noqa: PLC0415

        session_factory = _get_session_factory()
    async with session_factory() as session:
        account = await session.get(ConnectorAccountRow, account_id)
        if account is None:  # pragma: no cover - the account existed a moment ago
            return
        discord = _load_discord_meta()
        if discord is None:  # pragma: no cover - discord is always loaded in prod
            return
        await handle_discord_callback(
            raw_body=raw_body,
            account=account,
            session=session,
            discord=discord,
            cipher=cipher,
            dispatcher=dispatcher,
        )
        await session.commit()


async def process_discord_callback(
    *,
    raw_body: bytes,
    account: ConnectorAccountRow,
    session: AsyncSession,
    cipher: CredentialCipher,
    dispatcher: Any | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool | Response:
    """Route-facing entrypoint. For an approve/reject component tap, return the
    SYNCHRONOUS DEFERRED interaction response (``{"type": 6}``) and schedule the slow
    approve/dispatch/edit on a background task using a FRESH DB session — so the HTTP
    response lands well within Discord's ~3s window. Return ``False`` (route falls
    through to the PING/skip handshake path) when the body is not a component tap.

    The request ``session`` is NOT used for the state change (it is closed once the
    response is sent) — hence the background task's own session. ``session_factory``
    / ``dispatcher`` are injectable for tests; production leaves them ``None`` (a
    fresh session from the process factory + the real delivery dispatcher)."""
    del session  # discord settles on a FRESH background session, not the request one.
    body = _load_json(raw_body)
    if not isinstance(body, dict) or not _is_component_interaction(body):
        return False
    task = BackgroundTask(
        _run_discord_approval,
        account_id=account.id,
        raw_body=raw_body,
        cipher=cipher,
        dispatcher=dispatcher,
        session_factory=session_factory,
    )
    return JSONResponse(
        status_code=200,
        content={"type": DEFERRED_UPDATE_MESSAGE},
        background=task,
    )


__all__ = [
    "DEFERRED_UPDATE_MESSAGE",
    "DISCORD_ADAPTER",
    "handle_discord_callback",
    "process_discord_callback",
]
