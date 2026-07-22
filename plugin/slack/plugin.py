"""Slack connector plugin — capability registrations (Workflow §6 #4, §9).

The :class:`backend.extensions.plugin.PluginLoader` imports this file and picks up the
module-level ``p`` (a :class:`backend.extensions.plugin.PluginBuilder`). Every external
call goes through :class:`~.client.SlackClient`; the inbound parser lives in
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

from backend.extensions.plugin.context import SkillContext
from backend.workflow.domain.incoming import TriggerEvent
from bsvibe_sdk import plugin
from plugin.slack.client import DEFAULT_BASE_URL, SlackClient
from plugin.slack.webhook import parse_event, parse_interaction

p = plugin(
    name="slack",
    version="0.1.0",
    description="Slack connector — Events-API intake + chat message delivery with compensation.",
    author="BSVibe",
    data_jurisdiction="us",
    credentials=[
        {
            "name": "bot_token",
            "description": "Slack bot OAuth token (xoxb-…) with chat:write scope.",
            "required": True,
        },
        {
            "name": "signing_secret",
            "description": "Slack signing secret used to verify inbound event signatures.",
            "required": False,
        },
    ],
)


def _client(context: SkillContext) -> SlackClient:
    """Build an authed client from the injected credentials.

    ``config['slack_api_url']`` overrides the API base (testing / proxy).
    Raises ``ValueError`` (→ ``PluginRunError`` at the runner boundary) when no
    bot token credential is present.
    """
    token = context.credentials.get("bot_token")
    if not token:
        raise ValueError("slack: missing required 'bot_token' credential")
    base_url = context.config.get("slack_api_url", DEFAULT_BASE_URL)
    return SlackClient(token, base_url=base_url)


# ── inbound ──────────────────────────────────────────────────────────────────


@p.inbound(trigger={"type": "webhook"})
async def on_event(context: SkillContext, payload: dict[str, Any]) -> TriggerEvent | None:
    """Parse a Slack Events-API delivery into a TriggerEvent (or None to skip).

    Expected ``payload`` shape (populated by the intake webhook route — out of
    this track's scope)::

        {"workspace_id": UUID, "headers": {...}, "raw_body": bytes}
    """
    raw_body = payload["raw_body"]
    if isinstance(raw_body, str):
        raw_body = raw_body.encode()
    secret = context.credentials.get("signing_secret")
    return parse_event(
        workspace_id=payload["workspace_id"],
        headers=payload.get("headers", {}),
        raw_body=raw_body,
        secret=secret,
    )


# ── outbound ─────────────────────────────────────────────────────────────────


@p.outbound(
    artifact_types=["slack_message"],
    compensation_tier="t2_trail",
    compensation_supported=True,
)
async def deliver_message(context: SkillContext, event: dict[str, Any]) -> dict[str, Any]:
    """Post a Slack message. A posted message can be deleted (chat.delete),
    but deletion leaves an audit trail in Slack → tier ``t2_trail``."""
    channel = event["channel"]
    client = _client(context)
    data = await client.post_message(
        channel,
        event["text"],
        thread_ts=event.get("thread_ts"),
        blocks=event.get("blocks"),
    )
    ts = str(data["ts"])
    out_channel = str(data.get("channel", channel))
    return {
        "artifact_type": "slack_message",
        "external_ref": f"slack://{out_channel}/{ts}",
        "url": event.get("permalink"),
        "compensation_handle": {
            "kind": "message",
            "channel": out_channel,
            "ts": ts,
        },
    }


# ── compensation (idempotent — Workflow §9) ────────────────────────────────────


@p.compensate(artifact_types=["slack_message"])
async def revert_message(context: SkillContext, handle: dict[str, Any]) -> dict[str, Any]:
    """Delete the posted message (T2 — deletion leaves a Slack audit trail).
    Idempotent: an already-deleted message (``message_not_found``) yields a
    silent no-op success."""
    channel, ts = handle["channel"], str(handle["ts"])
    client = _client(context)
    error = await client.delete_message(channel, ts)
    already = error == "message_not_found"
    return {
        "status": "partially_compensated",
        "tier": "t2_trail",
        "already": already,
        "summary": (
            f"message {ts} already gone" if already else f"deleted message {ts} (trail remains)"
        ),
    }


# ── actions (agent-loop tools) ──────────────────────────────────────────────────


@p.action(
    name="post_message",
    mcp_exposed=True,
    input_schema={
        "type": "object",
        "required": ["channel", "text"],
        "properties": {
            "channel": {"type": "string", "description": "channel ID or name"},
            "text": {"type": "string"},
            "thread_ts": {"type": "string", "description": "parent message ts to reply in-thread"},
        },
        "additionalProperties": False,
    },
)
async def post_message(
    context: SkillContext,
    channel: str,
    text: str,
    thread_ts: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    client = _client(context)
    data = await client.post_message(channel, text, thread_ts=thread_ts, blocks=blocks)
    ts = str(data["ts"])
    out_channel = str(data.get("channel", channel))
    return {
        "ts": ts,
        "channel": out_channel,
        "external_ref": f"slack://{out_channel}/{ts}",
    }


# ── callback capabilities (inbound approve/reject) ──────────────────────────────
#
# The seam the backend inbound callback handler dispatches through: the founder-
# auth + Safe-Mode approve orchestration lives in the backend while ALL slack-
# specific parsing + Web-API calls stay here (dispatched via PluginRunner, never
# imported into ``backend.api``). ``mcp_exposed=False`` — internal capabilities.


@p.action(name="parse_slack_interaction")
async def parse_slack_interaction(
    context: SkillContext, body: dict[str, Any]
) -> dict[str, Any] | None:
    """Pure parse of a ``block_actions`` payload into founder-auth + action fields
    (see :func:`plugin.slack.webhook.parse_interaction`). No creds needed — the
    request was already gated by the webhook_token + signature. Returns ``None``
    when the body is not a block_actions interaction."""
    del context
    if not isinstance(body, dict) or body.get("type") != "block_actions":
        return None
    return parse_interaction(body)


@p.action(name="respond_ephemeral")
async def respond_ephemeral(
    context: SkillContext,
    response_url: str,
    text: str,
) -> dict[str, Any]:
    """Post an EPHEMERAL note to the tapper via the interactivity ``response_url``.

    Slack has no separate spinner-ack (HTTP 200 to the POST is the ack); this is
    how an unauthorized tapper is told "권한이 없어요" and how the acting founder gets
    a private confirmation — without editing the shared card. Best-effort UI ack."""
    client = _client(context)
    await client.respond(response_url, text)
    return {"ok": True}


@p.action(name="update_message")
async def update_message(
    context: SkillContext,
    channel: str,
    ts: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Edit the card in place (``chat.update``) — keep the original non-button
    blocks, append the approve/reject status block, and drop the buttons. ``text``
    is the accessibility / notification fallback."""
    client = _client(context)
    return await client.update_message(channel, ts, text, blocks=blocks)


# ── setup ────────────────────────────────────────────────────────────────────


@p.setup
async def setup(cred_store: Any) -> dict[str, Any]:
    """Credential flow for the slack connector.

    Reads ``SLACK_BOT_TOKEN`` (xoxb-… bot token) and the optional
    ``SLACK_SIGNING_SECRET`` from the environment and persists them under the
    ``slack`` namespace. Env-based ingestion keeps secrets out of shell
    history and process args (python-security) and stays non-interactive for
    CI / headless setup.
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise ValueError("slack setup: set SLACK_BOT_TOKEN (xoxb-… bot token) in the environment")
    data: dict[str, Any] = {"bot_token": token}
    secret = os.environ.get("SLACK_SIGNING_SECRET")
    if secret:
        data["signing_secret"] = secret
    await cred_store.store("slack", data)
    return data
