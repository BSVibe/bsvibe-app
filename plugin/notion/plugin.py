"""Notion connector plugin — capability registrations (Workflow §6 #4, §9).

The :class:`backend.extensions.plugin.PluginLoader` imports this file and picks up the
module-level ``p`` (a :class:`backend.extensions.plugin.PluginBuilder`). Every external
call goes through :class:`~.client.NotionClient`.

Outbound functions return a plain ``dict`` — the
:class:`backend.delivery.dispatcher.DeliveryDispatcher` wraps it into an
``ActionResult.output`` and builds the persisted ``DeliveryResult``. The dict
carries ``external_ref`` / ``url`` / ``compensation_handle`` (Workflow §3.1)
so the matching ``@p.compensate`` handler can later revert by handle.

A Notion page is a *new artifact*: once created it cannot be cleanly deleted
in place via the public API — the best undo is to archive (trash) it. Hence
the outbound declares ``compensation_tier="t3_new_artifact"`` (Workflow §9.1).
"""

from __future__ import annotations

import os
from typing import Any

from backend.extensions.plugin.context import SkillContext
from bsvibe_sdk import plugin
from plugin.notion.client import DEFAULT_BASE_URL, NotionClient

p = plugin(
    name="notion",
    version="0.1.0",
    description="Notion connector — create pages from deliverables with compensation (archive).",
    author="BSVibe",
    data_jurisdiction="us",
    credentials=[
        {
            "name": "token",
            "description": "Notion internal integration token (secret_...).",
            "required": True,
        },
    ],
)


def _client(context: SkillContext) -> NotionClient:
    """Build an authed client from the injected credentials.

    ``config['notion_api_url']`` overrides the API base; ``config['notion_version']``
    overrides the ``Notion-Version`` header. Raises ``ValueError`` (→
    ``PluginRunError`` at the runner boundary) when no token credential is present.
    """
    token = context.credentials.get("token")
    if not token:
        raise ValueError("notion: missing required 'token' credential")
    base_url = context.config.get("notion_api_url", DEFAULT_BASE_URL)
    version = context.config.get("notion_version")
    if version:
        return NotionClient(token, base_url=base_url, notion_version=version)
    return NotionClient(token, base_url=base_url)


def _parent_page_id(context: SkillContext, event: dict[str, Any]) -> str:
    """Resolve the parent page id from the event, falling back to config."""
    parent = event.get("parent_page_id") or context.config.get("notion_parent_page_id")
    if not parent:
        raise ValueError(
            "notion: missing 'parent_page_id' (event) / 'notion_parent_page_id' (config)"
        )
    return str(parent)


# ── outbound ─────────────────────────────────────────────────────────────────


@p.outbound(
    artifact_types=["page", "page_image"],
    compensation_tier="t3_new_artifact",
    compensation_supported=True,
)
async def deliver_page(context: SkillContext, event: dict[str, Any]) -> dict[str, Any]:
    """Create a Notion page from a deliverable under the configured parent page."""
    parent_page_id = _parent_page_id(context, event)
    client = _client(context)
    data = await client.create_page(
        parent_page_id=parent_page_id,
        title=event["title"],
        body=event.get("body", ""),
    )
    page_id = str(data["id"])
    return {
        "artifact_type": "page",
        "external_ref": f"notion://page/{page_id}",
        "url": data.get("url"),
        "compensation_handle": {
            "kind": "page",
            "page_id": page_id,
        },
    }


# ── compensation (idempotent — Workflow §9) ────────────────────────────────────


@p.compensate(artifact_types=["page", "page_image"])
async def revert_page(context: SkillContext, handle: dict[str, Any]) -> dict[str, Any]:
    """Archive the page (T3 — the page becomes a new, trashed artifact; the
    public API cannot hard-delete it in place). Idempotent: a 404 (page already
    gone) is treated as success."""
    page_id = str(handle["page_id"])
    client = _client(context)
    status = await client.archive_page(page_id)
    already = status == 404
    return {
        "status": "partially_compensated",
        "tier": "t3_new_artifact",
        "already": already,
        "summary": (f"page {page_id} already gone" if already else f"archived page {page_id}"),
    }


# ── actions (agent-loop tools) ──────────────────────────────────────────────────


@p.action(
    name="create_page",
    mcp_exposed=True,
    input_schema={
        "type": "object",
        "required": ["parent_page_id", "title"],
        "properties": {
            "parent_page_id": {"type": "string", "description": "id of the parent page"},
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        "additionalProperties": False,
    },
)
async def create_page(
    context: SkillContext,
    parent_page_id: str,
    title: str,
    body: str = "",
) -> dict[str, Any]:
    client = _client(context)
    data = await client.create_page(parent_page_id=parent_page_id, title=title, body=body)
    page_id = str(data["id"])
    return {
        "page_id": page_id,
        "url": data.get("url"),
        "external_ref": f"notion://page/{page_id}",
    }


@p.action(
    name="append",
    mcp_exposed=True,
    input_schema={
        "type": "object",
        "required": ["page_id", "text"],
        "properties": {
            "page_id": {"type": "string", "description": "id of the page/block to append to"},
            "text": {"type": "string"},
        },
        "additionalProperties": False,
    },
)
async def append(context: SkillContext, page_id: str, text: str) -> dict[str, Any]:
    client = _client(context)
    data = await client.append_block(page_id, text)
    return {"page_id": page_id, "appended": True, "object": data.get("object")}


# ── setup ────────────────────────────────────────────────────────────────────


@p.setup
async def setup(cred_store: Any) -> dict[str, Any]:
    """Credential flow for the notion connector.

    Reads ``NOTION_TOKEN`` (internal integration token) from the environment
    and persists it under the ``notion`` namespace. Env-based ingestion keeps
    secrets out of shell history and process args (python-security) and stays
    non-interactive for CI / headless setup.
    """
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise ValueError(
            "notion setup: set NOTION_TOKEN (internal integration token) in the environment"
        )
    data: dict[str, Any] = {"token": token}
    await cred_store.store("notion", data)
    return data
