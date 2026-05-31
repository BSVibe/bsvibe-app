"""Discord connector plugin — capability registrations (Workflow §6 #4, §9).

The :class:`backend.extensions.plugin.PluginLoader` imports this file and picks up the
module-level ``p`` (a :class:`backend.extensions.plugin.PluginBuilder`). Every external
call goes through :class:`~.client.DiscordClient`; the inbound parser lives in
:mod:`~.webhook`.

Outbound functions return a plain ``dict`` — the
:class:`backend.delivery.dispatcher.DeliveryDispatcher` wraps it into an
``ActionResult.output`` and builds the persisted ``DeliveryResult``. The dict
carries ``external_ref`` / ``url`` / ``compensation_handle`` (Workflow §3.1) so
the matching ``@p.compensate`` handler can later revert by handle.
"""

from __future__ import annotations

import os
from typing import Any

from backend.extensions.plugin.context import SkillContext
from backend.workflow.domain.incoming import TriggerEvent
from bsvibe_sdk import plugin
from plugin.discord.client import DEFAULT_BASE_URL, DiscordClient
from plugin.discord.webhook import parse_interaction

p = plugin(
    name="discord",
    version="0.1.0",
    description="Discord connector — interaction webhook intake + channel message delivery.",
    author="BSVibe",
    # Discord Inc. is US-headquartered and serves traffic from multiple regions
    # behind a US-operated control plane — the concrete "us" value matches the
    # github/slack connectors (and is more accurate than "unknown" since the
    # operator/jurisdiction is well-defined, unlike Telegram). See
    # VALID_JURISDICTIONS in backend/plugins/base.py.
    data_jurisdiction="us",
    credentials=[
        {
            "name": "bot_token",
            "description": "Discord bot token (Bot <token>) with send/manage message perms.",
            "required": True,
        },
        {
            "name": "public_key",
            "description": (
                "Discord application Ed25519 public key (hex) used to verify "
                "inbound interaction signatures (X-Signature-Ed25519)."
            ),
            "required": False,
        },
    ],
)


def _client(context: SkillContext) -> DiscordClient:
    """Build an authed client from the injected credentials.

    ``config['discord_api_url']`` overrides the API base (testing / proxy).
    Raises ``ValueError`` (→ ``PluginRunError`` at the runner boundary) when no
    bot token credential is present.
    """
    token = context.credentials.get("bot_token")
    if not token:
        raise ValueError("discord: missing required 'bot_token' credential")
    base_url = context.config.get("discord_api_url", DEFAULT_BASE_URL)
    return DiscordClient(token, base_url=base_url)


# ── inbound ──────────────────────────────────────────────────────────────────


@p.inbound(trigger={"type": "webhook"})
async def on_interaction(context: SkillContext, payload: dict[str, Any]) -> TriggerEvent | None:
    """Parse a Discord interaction webhook into a TriggerEvent (or None to skip).

    Expected ``payload`` shape (populated by the intake webhook route — out of
    this track's scope)::

        {"workspace_id": UUID, "headers": {...}, "raw_body": bytes}
    """
    raw_body = payload["raw_body"]
    if isinstance(raw_body, str):
        raw_body = raw_body.encode()
    public_key = context.credentials.get("public_key")
    return parse_interaction(
        workspace_id=payload["workspace_id"],
        headers=payload.get("headers", {}),
        raw_body=raw_body,
        public_key=public_key,
    )


# ── outbound ─────────────────────────────────────────────────────────────────


@p.outbound(
    artifact_types=["discord_message"],
    compensation_tier="t2_trail",
    compensation_supported=True,
)
async def deliver_message(context: SkillContext, event: dict[str, Any]) -> dict[str, Any]:
    """Post a Discord channel message. A posted message can be removed with a
    ``DELETE`` call, but a recipient may already have seen it and the deletion is
    audit-visible (no clean undo) → tier ``t2_trail`` (matches Slack/Telegram's
    deletable-message tier)."""
    channel_id = str(event["channel_id"])
    client = _client(context)
    data = await client.create_message(channel_id, event["content"])
    message_id = str(data["id"])
    out_channel = str(data.get("channel_id", channel_id))
    return {
        "artifact_type": "discord_message",
        "external_ref": f"discord://{out_channel}/{message_id}",
        "url": event.get("permalink"),
        "compensation_handle": {
            "kind": "message",
            "channel_id": out_channel,
            "message_id": message_id,
        },
    }


# ── compensation (idempotent — Workflow §9) ────────────────────────────────────


@p.compensate(artifact_types=["discord_message"])
async def revert_message(context: SkillContext, handle: dict[str, Any]) -> dict[str, Any]:
    """Delete the posted message (T2 — a recipient may already have seen it, so
    the deletion is not a clean undo). Idempotent: an already-deleted message
    (HTTP 404) yields a silent no-op success."""
    channel_id, message_id = str(handle["channel_id"]), str(handle["message_id"])
    client = _client(context)
    status = await client.delete_message(channel_id, message_id)
    already = status == 404
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
        "required": ["channel_id", "content"],
        "properties": {
            "channel_id": {"type": "string", "description": "target Discord channel ID"},
            "content": {"type": "string"},
        },
        "additionalProperties": False,
    },
)
async def send_message(
    context: SkillContext,
    channel_id: str,
    content: str,
) -> dict[str, Any]:
    client = _client(context)
    data = await client.create_message(channel_id, content)
    message_id = str(data["id"])
    out_channel = str(data.get("channel_id", channel_id))
    return {
        "message_id": message_id,
        "channel_id": out_channel,
        "external_ref": f"discord://{out_channel}/{message_id}",
    }


# ── setup ────────────────────────────────────────────────────────────────────


@p.setup
async def setup(cred_store: Any) -> dict[str, Any]:
    """Credential flow for the discord connector.

    Reads ``DISCORD_BOT_TOKEN`` (the bot token) and the optional
    ``DISCORD_PUBLIC_KEY`` (the application's Ed25519 public key, hex) from the
    environment and persists them under the ``discord`` namespace. Env-based
    ingestion keeps secrets out of shell history and process args
    (python-security) and stays non-interactive for CI / headless setup.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("discord setup: set DISCORD_BOT_TOKEN (bot token) in the environment")
    data: dict[str, Any] = {"bot_token": token}
    public_key = os.environ.get("DISCORD_PUBLIC_KEY")
    if public_key:
        data["public_key"] = public_key
    await cred_store.store("discord", data)
    return data
