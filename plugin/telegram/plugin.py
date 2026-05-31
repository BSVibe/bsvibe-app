"""Telegram connector plugin — capability registrations (Workflow §6 #4, §9).

The :class:`backend.extensions.plugin.PluginLoader` imports this file and picks up the
module-level ``p`` (a :class:`backend.extensions.plugin.PluginBuilder`). Every external
call goes through :class:`~.client.TelegramClient`; the inbound parser lives in
:mod:`~.webhook`.

Outbound functions return a plain ``dict`` — the
:class:`backend.delivery.dispatcher.DeliveryDispatcher` wraps it into an
``ActionResult.output`` and builds the persisted ``DeliveryResult``. The dict
carries ``external_ref`` / ``url`` / ``compensation_handle`` (Workflow §3.1)
so the matching ``@p.compensate`` handler can later revert by handle.
"""

from __future__ import annotations

import os
from typing import Any

from backend.extensions.plugin import plugin
from backend.extensions.plugin.context import SkillContext
from backend.intake.schema import TriggerEvent
from plugin.telegram.client import DEFAULT_BASE_URL, TelegramClient
from plugin.telegram.webhook import parse_update

p = plugin(
    name="telegram",
    version="0.1.0",
    description="Telegram connector — Bot-API webhook intake + message delivery with compensation.",
    author="BSVibe",
    # Telegram is operated out of multiple regions (no single jurisdiction);
    # the framework's "unspecified/global" value is "unknown" (the others use
    # the concrete "us"). See VALID_JURISDICTIONS in backend/plugins/base.py.
    data_jurisdiction="unknown",
    credentials=[
        {
            "name": "bot_token",
            "description": "Telegram Bot API token (from @BotFather).",
            "required": True,
        },
        {
            "name": "webhook_secret",
            "description": (
                "Secret token configured via setWebhook; echoed back in the "
                "X-Telegram-Bot-Api-Secret-Token header on every delivery."
            ),
            "required": False,
        },
    ],
)


def _client(context: SkillContext) -> TelegramClient:
    """Build an authed client from the injected credentials.

    ``config['telegram_api_url']`` overrides the API base (testing / proxy).
    Raises ``ValueError`` (→ ``PluginRunError`` at the runner boundary) when no
    bot token credential is present.
    """
    token = context.credentials.get("bot_token")
    if not token:
        raise ValueError("telegram: missing required 'bot_token' credential")
    base_url = context.config.get("telegram_api_url", DEFAULT_BASE_URL)
    return TelegramClient(token, base_url=base_url)


# ── inbound ──────────────────────────────────────────────────────────────────


@p.inbound(trigger={"type": "webhook"})
async def on_update(context: SkillContext, payload: dict[str, Any]) -> TriggerEvent | None:
    """Parse a Telegram webhook Update into a TriggerEvent (or None to skip).

    Expected ``payload`` shape (populated by the intake webhook route — out of
    this track's scope)::

        {"workspace_id": UUID, "headers": {...}, "raw_body": bytes}
    """
    raw_body = payload["raw_body"]
    if isinstance(raw_body, str):
        raw_body = raw_body.encode()
    secret = context.credentials.get("webhook_secret")
    return parse_update(
        workspace_id=payload["workspace_id"],
        headers=payload.get("headers", {}),
        raw_body=raw_body,
        secret=secret,
    )


# ── outbound ─────────────────────────────────────────────────────────────────


@p.outbound(
    artifact_types=["telegram_message"],
    compensation_tier="t2_trail",
    compensation_supported=True,
)
async def deliver_message(context: SkillContext, event: dict[str, Any]) -> dict[str, Any]:
    """Send a Telegram message. A sent message can be removed with
    ``deleteMessage``, but only within 48h and the deletion leaves no trace for
    a recipient who already saw it (no clean undo) → tier ``t2_trail`` (matches
    Slack's deletable-message tier)."""
    chat_id = event["chat_id"]
    client = _client(context)
    data = await client.send_message(chat_id, event["text"])
    message_id = int(data["message_id"])
    out_chat = (data.get("chat") or {}).get("id", chat_id)
    return {
        "artifact_type": "telegram_message",
        "external_ref": f"telegram://{out_chat}/{message_id}",
        "url": event.get("permalink"),
        "compensation_handle": {
            "kind": "message",
            "chat_id": out_chat,
            "message_id": message_id,
        },
    }


# ── compensation (idempotent — Workflow §9) ────────────────────────────────────


@p.compensate(artifact_types=["telegram_message"])
async def revert_message(context: SkillContext, handle: dict[str, Any]) -> dict[str, Any]:
    """Delete the sent message (T2 — a recipient may already have seen it, so
    the deletion is not a clean undo). Idempotent: an already-deleted message
    ("message to delete not found") yields a silent no-op success."""
    chat_id, message_id = handle["chat_id"], int(handle["message_id"])
    client = _client(context)
    description = await client.delete_message(chat_id, message_id)
    already = description is not None
    return {
        "status": "partially_compensated",
        "tier": "t2_trail",
        "already": already,
        "summary": (
            f"message {message_id} already gone"
            if already
            else f"deleted message {message_id} (recipient may have seen it)"
        ),
    }


# ── actions (agent-loop tools) ──────────────────────────────────────────────────


@p.action(
    name="send_message",
    mcp_exposed=True,
    input_schema={
        "type": "object",
        "required": ["chat_id", "text"],
        "properties": {
            "chat_id": {
                "type": ["string", "integer"],
                "description": "target chat ID or @channelusername",
            },
            "text": {"type": "string"},
        },
        "additionalProperties": False,
    },
)
async def send_message(
    context: SkillContext,
    chat_id: str | int,
    text: str,
) -> dict[str, Any]:
    client = _client(context)
    data = await client.send_message(chat_id, text)
    message_id = int(data["message_id"])
    out_chat = (data.get("chat") or {}).get("id", chat_id)
    return {
        "message_id": message_id,
        "chat_id": out_chat,
        "external_ref": f"telegram://{out_chat}/{message_id}",
    }


# ── setup ────────────────────────────────────────────────────────────────────


@p.setup
async def setup(cred_store: Any) -> dict[str, Any]:
    """Credential flow for the telegram connector.

    Reads ``TELEGRAM_BOT_TOKEN`` (the @BotFather token) and the optional
    ``TELEGRAM_WEBHOOK_SECRET`` from the environment and persists them under the
    ``telegram`` namespace. Env-based ingestion keeps secrets out of shell
    history and process args (python-security) and stays non-interactive for
    CI / headless setup.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError(
            "telegram setup: set TELEGRAM_BOT_TOKEN (@BotFather token) in the environment"
        )
    data: dict[str, Any] = {"bot_token": token}
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if secret:
        data["webhook_secret"] = secret
    await cred_store.store("telegram", data)
    return data
