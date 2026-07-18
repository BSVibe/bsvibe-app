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

import httpx
import structlog

from backend.extensions.plugin.context import SkillContext
from bsvibe_sdk import plugin
from plugin.notion.client import DEFAULT_BASE_URL, NotionClient
from plugin.notion.converter import extract_page_title, render_blocks

logger = structlog.get_logger(__name__)

# Audit / structured-log event emitted on every successful knowledge
# import. The string is the spec-mandated identifier
# (``audit.knowledge.imported.notion``) so log searches + future audit
# relays can route on it deterministically.
AUDIT_EVENT_IMPORTED = "audit.knowledge.imported.notion"

# Bounded recursion into block trees: Notion's API requires per-level
# /blocks/{id}/children calls but real pages rarely nest deeper than 3-4
# layers. The cap defends against pathological tree depths.
_MAX_BLOCK_DEPTH = 8

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


# ── import_pages (Lift Q3-Notion — knowledge ingest) ───────────────────────


async def _hydrate_block_children(
    client: NotionClient,
    block: dict[str, Any],
    depth: int,
) -> None:
    """Recursively fetch ``/blocks/{id}/children`` for any nested blocks.

    The converter expects ``has_children`` blocks to carry a populated
    ``children`` list; we attach it here so render stays pure (no I/O).
    Gotcha: ``child_page`` blocks point at OTHER pages — we stop at them
    so we never recurse into unrelated content (spec gotcha #4).
    """
    if not block.get("has_children"):
        return
    if block.get("type") == "child_page":
        return
    if depth >= _MAX_BLOCK_DEPTH:
        return
    block_id = block.get("id")
    if not block_id:
        return
    children: list[dict[str, Any]] = []
    async for child in client.list_block_children(block_id):
        children.append(child)
        await _hydrate_block_children(client, child, depth + 1)
    block["children"] = children


async def _fetch_page_markdown(client: NotionClient, page_id: str) -> tuple[str, int]:
    """Pull every block under a page, hydrate children, render markdown.

    Returns ``(markdown, blocks_count)`` so the action can surface
    cumulative block totals in its summary + audit event.
    """
    top_blocks: list[dict[str, Any]] = []
    async for block in client.list_block_children(page_id):
        top_blocks.append(block)
        await _hydrate_block_children(client, block, depth=1)

    blocks_count = _count_blocks(top_blocks)
    return render_blocks(top_blocks), blocks_count


def _count_blocks(blocks: list[dict[str, Any]]) -> int:
    """Sum a block tree's node count (parents + recursive children)."""
    total = 0
    for block in blocks:
        total += 1
        children = block.get("children") or []
        if children:
            total += _count_blocks(children)
    return total


@p.action(
    name="import_pages",
    mcp_exposed=True,
    import_trigger=True,
    input_schema={
        "type": "object",
        "required": [],
        "properties": {
            "binding_id": {
                "type": "string",
                "description": (
                    "Identifier of the BSage binding the import is scoped"
                    " to. Used as the ``source_ref`` prefix so re-imports"
                    " hit IngestCompiler's content-hash dedup."
                ),
            },
            "region": {
                "type": "string",
                "description": (
                    "BSage region the seeds ingest into. Overrides binding"
                    " config's ``default_region`` (which itself defaults to"
                    " ``imported-notion``)."
                ),
            },
            "database_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of Notion database ids to scan. When"
                    " omitted, falls back to binding config's"
                    " ``database_ids`` or to ``/search`` (every accessible"
                    " page)."
                ),
            },
        },
        "additionalProperties": False,
    },
)
async def import_pages(
    context: SkillContext,
    binding_id: str | None = None,
    region: str | None = None,
    database_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Walk the connected Notion workspace and seed every page.

    When ``database_ids`` is set (here or in the binding config), each
    database is queried and every page therein is imported. Otherwise we
    fall back to the workspace-wide ``/search`` endpoint (filtered to
    page objects only — spec gotcha #2). Each page becomes one
    ``write_seed`` call under the ``notion`` source.

    Soft-fail per page: any error during a single page's block fetch is
    logged and the page is counted under ``skipped`` so a partial outage
    never poisons the whole batch.
    """
    knowledge = getattr(context, "knowledge", None)
    if knowledge is None:
        raise ValueError(
            "notion.import_pages: SkillContext.knowledge is required but"
            " was not injected (worker bootstrap should wire the garden)"
        )

    resolved_binding = binding_id or context.config.get("binding_id") or "default"
    resolved_region = region or context.config.get("default_region") or "imported-notion"
    resolved_dbs = database_ids
    if resolved_dbs is None:
        resolved_dbs = context.config.get("database_ids")

    client = _client(context)

    pages_count = 0
    blocks_count = 0
    skipped = 0

    async def _ingest_page(page: dict[str, Any]) -> None:
        nonlocal pages_count, blocks_count, skipped
        page_id = page.get("id")
        if not page_id:
            skipped += 1
            return
        try:
            markdown, page_blocks = await _fetch_page_markdown(client, page_id)
        except httpx.HTTPStatusError:
            # Most likely 404 (page removed mid-import) or 5xx; either way
            # we soft-skip rather than abort the whole batch.
            logger.warning(
                "notion_page_fetch_failed",
                page_id=page_id,
                exc_info=True,
            )
            skipped += 1
            return

        title = extract_page_title(page) or page_id
        properties = page.get("properties") or {}
        seed_data: dict[str, Any] = {
            "title": title,
            "content": markdown,
            "region": resolved_region,
            # Stable provenance — re-imports of the same page hit the
            # IngestCompiler content-hash dedup on the same key.
            "source_ref": f"notion://{resolved_binding}/{page_id}",
            "frontmatter": {
                "notion_page_id": page_id,
                "url": page.get("url"),
                # Carry the raw properties dict through unchanged so
                # downstream canonicalization can read original fields
                # without re-querying Notion.
                "properties": properties,
            },
        }

        try:
            await knowledge.write_seed("notion", seed_data)
        except Exception:  # noqa: BLE001 — soft-fail per page
            logger.warning(
                "notion_seed_write_failed",
                page_id=page_id,
                exc_info=True,
            )
            skipped += 1
            return

        pages_count += 1
        blocks_count += page_blocks

    if resolved_dbs:
        for db_id in resolved_dbs:
            async for page in client.query_database(db_id):
                await _ingest_page(page)
    else:
        async for page in client.search_pages():
            await _ingest_page(page)

    logger.info(
        AUDIT_EVENT_IMPORTED,
        binding_id=resolved_binding,
        region=resolved_region,
        pages_count=pages_count,
        blocks_count=blocks_count,
        skipped=skipped,
    )

    return {
        "pages_count": pages_count,
        "blocks_count": blocks_count,
        "skipped": skipped,
        "region": resolved_region,
    }


# ── setup ────────────────────────────────────────────────────────────────────


@p.setup
async def setup(cred_store: Any) -> dict[str, Any]:
    """Credential flow for the notion connector.

    Reads ``NOTION_TOKEN`` (internal integration token) from the environment
    and persists it under the ``notion`` namespace. Optional env vars:

    * ``NOTION_DATABASE_IDS`` — comma-separated database ids to import
      from instead of the whole accessible workspace.
    * ``NOTION_DEFAULT_REGION`` — BSage region seeds ingest into when
      ``import_pages`` is called without an explicit ``region`` kwarg
      (defaults to ``imported-notion`` if not set).

    Env-based ingestion keeps secrets out of shell history / process
    args (python-security) and stays non-interactive for CI / headless
    setup.
    """
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise ValueError(
            "notion setup: set NOTION_TOKEN (internal integration token) in the environment"
        )
    data: dict[str, Any] = {"token": token}
    db_ids_raw = os.environ.get("NOTION_DATABASE_IDS")
    if db_ids_raw:
        data["database_ids"] = [s.strip() for s in db_ids_raw.split(",") if s.strip()]
    region = os.environ.get("NOTION_DEFAULT_REGION")
    if region:
        data["default_region"] = region
    await cred_store.store("notion", data)
    return data
