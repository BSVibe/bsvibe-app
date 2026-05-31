"""Trello connector plugin — capability registrations (Workflow §6 #4, §9).

The :class:`backend.extensions.plugin.PluginLoader` imports this file and picks up the
module-level ``p`` (a :class:`backend.extensions.plugin.PluginBuilder`). Every external
call goes through :class:`~.client.TrelloClient` (Trello REST API).

Outbound functions return a plain ``dict`` — the
:class:`backend.delivery.dispatcher.DeliveryDispatcher` wraps it into an
``ActionResult.output`` and builds the persisted ``DeliveryResult``. The dict
carries ``external_ref`` / ``url`` / ``compensation_handle`` (Workflow §3.1)
so the matching ``@p.compensate`` handler can later revert by handle.

A Trello card is a *new artifact*: once created it cannot be cleanly
hard-deleted in place via the normal delivery flow — the best undo is to
archive (close) it. Hence the outbound declares
``compensation_tier="t3_new_artifact"`` (Workflow §9.1), mirroring the
notion created-page and linear created-issue tiers.

There is no ``@p.inbound`` capability: this connector is delivery-only.

All external I/O goes through :class:`~.client.TrelloClient` (httpx); tests
mock httpx and never reach real Trello.
"""

from __future__ import annotations

import os
from typing import Any

from backend.extensions.plugin import plugin
from backend.extensions.plugin.context import SkillContext
from plugin.trello.client import (
    DEFAULT_BASE_URL,
    TrelloApiError,
    TrelloClient,
)

p = plugin(
    name="trello",
    version="0.1.0",
    description="Trello connector — create cards from deliverables with compensation (archive).",
    author="BSVibe",
    data_jurisdiction="us",
    credentials=[
        {
            "name": "api_key",
            "description": "Trello API key, sent as the 'key' query parameter.",
            "required": True,
        },
        {
            "name": "token",
            "description": "Trello API token, sent as the 'token' query parameter.",
            "required": True,
        },
    ],
)


def _client(context: SkillContext) -> TrelloClient:
    """Build an authed client from the injected credentials.

    ``config['trello_api_url']`` overrides the API base. Raises ``ValueError``
    (→ ``PluginRunError`` at the runner boundary) when the api_key/token
    credentials are missing.
    """
    api_key = context.credentials.get("api_key")
    token = context.credentials.get("token")
    if not api_key or not token:
        raise ValueError("trello: missing required 'api_key' / 'token' credentials")
    base_url = context.config.get("trello_api_url", DEFAULT_BASE_URL)
    return TrelloClient(api_key, token, base_url=base_url)


def _list_id(context: SkillContext, event: dict[str, Any]) -> str:
    """Resolve the Trello list id from the event, falling back to config."""
    list_id = event.get("list_id") or context.config.get("trello_list_id")
    if not list_id:
        raise ValueError("trello: missing 'list_id' (event) / 'trello_list_id' (config)")
    return str(list_id)


# ── outbound ─────────────────────────────────────────────────────────────────


@p.outbound(
    artifact_types=["card"],
    compensation_tier="t3_new_artifact",
    compensation_supported=True,
)
async def deliver_card(context: SkillContext, event: dict[str, Any]) -> dict[str, Any]:
    """Create a Trello card from a deliverable on the resolved list."""
    list_id = _list_id(context, event)
    client = _client(context)
    desc = event.get("desc")
    if desc is None:
        desc = event.get("body", "")
    card = await client.create_card(
        list_id=list_id,
        name=event["title"],
        desc=desc,
    )
    card_id = str(card["id"])
    return {
        "artifact_type": "card",
        "external_ref": f"trello://card/{card_id}",
        "url": card.get("shortUrl") or card.get("url"),
        "compensation_handle": {
            "kind": "card",
            "card_id": card_id,
        },
    }


# ── compensation (idempotent — Workflow §9) ────────────────────────────────────


@p.compensate(artifact_types=["card"])
async def revert_card(context: SkillContext, handle: dict[str, Any]) -> dict[str, Any]:
    """Archive the card (T3 — the card becomes a new, closed artifact; the
    delivery flow cannot hard-delete it in place). Idempotent: an
    already-closed card or a 404 (card already gone) is treated as success."""
    card_id = str(handle["card_id"])
    client = _client(context)
    status = await client.archive_card(card_id)
    already = status == 404
    return {
        "status": "partially_compensated",
        "tier": "t3_new_artifact",
        "already": already,
        "summary": (f"card {card_id} already gone" if already else f"archived card {card_id}"),
    }


# ── actions (agent-loop tools) ──────────────────────────────────────────────────


@p.action(
    name="create_card",
    mcp_exposed=True,
    input_schema={
        "type": "object",
        "required": ["list_id", "title"],
        "properties": {
            "list_id": {"type": "string", "description": "Trello list id"},
            "title": {"type": "string"},
            "desc": {"type": "string"},
        },
        "additionalProperties": False,
    },
)
async def create_card(
    context: SkillContext,
    list_id: str,
    title: str,
    desc: str = "",
) -> dict[str, Any]:
    client = _client(context)
    card = await client.create_card(list_id=list_id, name=title, desc=desc)
    card_id = str(card["id"])
    return {
        "card_id": card_id,
        "url": card.get("shortUrl") or card.get("url"),
        "external_ref": f"trello://card/{card_id}",
    }


# ── setup ────────────────────────────────────────────────────────────────────


@p.setup
async def setup(cred_store: Any) -> dict[str, Any]:
    """Credential flow for the trello connector.

    Reads ``TRELLO_API_KEY`` + ``TRELLO_TOKEN`` and the optional default
    ``TRELLO_LIST_ID`` from the environment and persists them under the
    ``trello`` namespace. Env-based ingestion keeps secrets out of shell
    history and process args (python-security) and stays non-interactive for
    CI / headless setup.
    """
    api_key = os.environ.get("TRELLO_API_KEY")
    token = os.environ.get("TRELLO_TOKEN")
    if not api_key or not token:
        raise ValueError("trello setup: set TRELLO_API_KEY and TRELLO_TOKEN in the environment")
    data: dict[str, Any] = {"api_key": api_key, "token": token}
    list_id = os.environ.get("TRELLO_LIST_ID")
    if list_id:
        data["trello_list_id"] = list_id
    await cred_store.store("trello", data)
    return data


# Re-export so the compensation error type is importable from the plugin module
# (mirrors linear/notion module surface; keeps the runner boundary clean).
__all__ = ["TrelloApiError", "p"]
