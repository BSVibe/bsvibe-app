"""Telegram adapter for the connector-agnostic interactive-approval handler.

A founder taps Approve / Reject on the "작업 완료" (shipped) card; this settles the
held Safe-Mode item straight from Telegram — no PWA round trip. The connector-
NEUTRAL flow (resolve item, approve/deny + dispatch, audit actor, keep-body edit,
idempotency, best-effort ack) lives in
:mod:`backend.connectors.approval_callback`; this module is the thin TELEGRAM
ADAPTER — the telegram-specific founder-auth, the ``callback_query`` body
predicate, the ``bot_token`` credential key + ``@p.action`` names, and the
kwargs-builders for the ``answerCallbackQuery`` / ``editMessageText`` Bot-API
calls.

Boundary discipline (Lift Q3 / R2c): the route + this module never import
``plugin.telegram``. ALL telegram-specific work — parsing the callback_query and
the Bot-API calls — lives in the telegram plugin and is reached through
:class:`PluginRunner` + :class:`PluginMeta` (the sanctioned backend→plugin
dispatch seam). The plugin is loaded at call time via importlib (see
:func:`_load_telegram_meta`) so ``backend.api.webhooks`` stays free of any
transitive ``plugin`` edge.

Security (do the auth BEFORE any state change): a tap is honoured only when the
chat is PRIVATE and ``callback_query.from.id`` equals the account's bound
``delivery_config['chat_id']`` (the founder's 1:1 chat) — see
:func:`_is_authorized_founder`.
"""

from __future__ import annotations

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

# editMessageText payload that DROPS the card's inline buttons (empty keyboard).
_NO_KEYBOARD: dict[str, Any] = {"inline_keyboard": []}


def _is_callback_query(body: dict[str, Any]) -> bool:
    """The inbound body is an approve/reject tap iff it carries a ``callback_query``."""
    return "callback_query" in body


def _is_authorized_founder(parsed: dict[str, Any], account: ConnectorAccountRow) -> bool:
    """The tap is the authorized founder iff the chat is PRIVATE and the tapper's
    ``from.id`` equals the account's bound ``chat_id`` (compared as strings, since
    ``delivery_config`` may store either an int or a str)."""
    if parsed.get("chat_type") != "private":
        return False
    bound_chat_id = account.delivery_config.get("chat_id")
    if bound_chat_id is None:
        return False
    return str(parsed.get("from_id")) == str(bound_chat_id)


def _build_ack(parsed: dict[str, Any], text: str) -> dict[str, Any] | None:
    """``answerCallbackQuery`` kwargs. ``None`` when there is no query id to ack."""
    callback_query_id = parsed.get("callback_query_id")
    if callback_query_id is None:  # pragma: no cover - always present on a real tap
        return None
    return {"callback_query_id": callback_query_id, "text": text}


def _build_update(parsed: dict[str, Any], status: str) -> dict[str, Any] | None:
    """``editMessageText`` kwargs that make the card read like HISTORY: KEEP the
    original body, APPEND the approve/reject ``status`` line after it, and DROP the
    buttons. The original ``entities`` are re-sent unchanged — appending at the END
    keeps every original offset valid, so the "보고서 보기" ``text_link`` hyperlink is
    preserved.

    Falls back to the ``status`` line alone (no entities) when the callback carries
    no message text (shouldn't happen for a real card). Missing chat/message ids →
    ``None`` (nothing to edit)."""
    chat_id = parsed.get("chat_id")
    message_id = parsed.get("message_id")
    if chat_id is None or message_id is None:  # pragma: no cover
        return None
    original_text = parsed.get("message_text")
    if original_text:
        text = f"{original_text}\n\n{status}"
        entities = parsed.get("message_entities")
    else:
        text = status
        entities = None
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": _NO_KEYBOARD,
    }
    if entities is not None:
        kwargs["entities"] = entities
    return kwargs


# The telegram surface the connector-agnostic handler plugs into.
TELEGRAM_ADAPTER = ApprovalConnectorAdapter(
    connector="telegram",
    credential_key="bot_token",
    parse_action="parse_callback",
    ack_action="answer_callback_query",
    update_action="edit_message_text",
    is_interaction=_is_callback_query,
    is_authorized=_is_authorized_founder,
    build_ack=_build_ack,
    build_update=_build_update,
)


async def handle_telegram_callback(
    *,
    raw_body: bytes,
    account: ConnectorAccountRow,
    session: AsyncSession,
    telegram: PluginMeta,
    cipher: CredentialCipher,
    dispatcher: Any | None = None,
    runner: PluginRunner | None = None,
) -> bool:
    """Handle one telegram ``callback_query`` approve/reject tap by delegating to
    the connector-agnostic :func:`handle_approval_callback` with the telegram
    adapter.

    Returns ``True`` when the body was a callback_query and was handled (the route
    replies 200), ``False`` when the body is NOT a callback_query (the route falls
    through to its normal handshake/skip handling)."""
    return await handle_approval_callback(
        adapter=TELEGRAM_ADAPTER,
        raw_body=raw_body,
        account=account,
        session=session,
        plugin=telegram,
        cipher=cipher,
        dispatcher=dispatcher,
        runner=runner,
    )


@lru_cache(maxsize=1)
def _load_telegram_meta() -> PluginMeta | None:
    """The loaded telegram :class:`PluginMeta` (cached). ``None`` if the plugin is
    absent. Loads via importlib at call time — no static ``plugin.telegram`` edge
    (so ``backend.api.webhooks`` stays free of the reverse coupling)."""
    from backend.extensions.plugin.loader import PluginLoader  # noqa: PLC0415

    plugins_dir = Path(__file__).resolve().parents[2] / "plugin"
    registry = PluginLoader(plugins_dir).load_all_sync()
    return registry.get("telegram")


async def process_telegram_callback(
    *,
    raw_body: bytes,
    account: ConnectorAccountRow,
    session: AsyncSession,
    cipher: CredentialCipher,
) -> bool:
    """Route-facing entrypoint: load the telegram plugin + delegate to
    :func:`handle_telegram_callback` with the production dispatcher. Returns
    ``False`` (route falls through) when the body is not a callback_query or the
    telegram plugin is unavailable."""
    telegram = _load_telegram_meta()
    if telegram is None:  # pragma: no cover - telegram is always loaded in prod
        return False
    return await handle_telegram_callback(
        raw_body=raw_body,
        account=account,
        session=session,
        telegram=telegram,
        cipher=cipher,
    )


__all__ = [
    "TELEGRAM_ADAPTER",
    "handle_telegram_callback",
    "process_telegram_callback",
]
