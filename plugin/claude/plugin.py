"""Claude conversation export connector — capability registrations.

The :class:`backend.extensions.plugin.PluginLoader` imports this file and
picks up the module-level ``p`` (a :class:`PluginBuilder`). The
``import_conversations`` action reads the local ``conversations.json``
file from claude.ai's export bundle, parses + renders each conversation
to markdown, and submits each as a seed via the restricted garden
surface (``context.knowledge.write_seed``).

No outbound dispatch / compensate / webhook intake — Claude export is a
one-way inbound knowledge ingest. Pattern mirrors ``plugin.obsidian``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import structlog

from backend.extensions.plugin.context import SkillContext
from bsvibe_sdk import plugin
from plugin.claude.parser import parse_export
from plugin.claude.renderer import render_frontmatter_only, render_markdown

logger = structlog.get_logger(__name__)

# Audit / structured-log event emitted on every successful import. The
# string is the spec-mandated identifier (``audit.knowledge.imported.claude``)
# so log searches + future audit relays can route on it deterministically.
AUDIT_EVENT_IMPORTED = "audit.knowledge.imported.claude"


p = plugin(
    name="claude",
    version="0.1.0",
    description="Claude conversation export knowledge import — seeds BSage from claude.ai export bundle.",
    author="BSVibe",
    # The export file sits on the founder's local machine; no cloud
    # residency boundary applies — local-only data.
    data_jurisdiction="local",
    # No external API credentials; the binding config carries export_path.
    credentials=[],
)


def _resolve_export_file(export_path: str) -> Path:
    """Resolve the path to ``conversations.json``.

    ``export_path`` may be either the JSON file itself or the directory
    containing it (the export bundle is shipped as a folder).
    """
    path = Path(export_path)
    if path.is_dir():
        return path / "conversations.json"
    return path


@p.action(
    name="import_conversations",
    mcp_exposed=True,
    input_schema={
        "type": "object",
        "required": [],
        "properties": {
            "claude_binding_id": {
                "type": "string",
                "description": (
                    "Identifier of the BSage binding the import is scoped"
                    " to. Used as the ``source_ref`` prefix so re-imports"
                    " hit IngestCompiler's content-hash dedup."
                ),
            },
            "export_path": {
                "type": "string",
                "description": (
                    "Absolute path to ``conversations.json`` (or the"
                    " directory containing it). Falls back to the binding"
                    " config's ``export_path`` when omitted."
                ),
            },
            "since": {
                "type": "string",
                "description": (
                    "Optional ISO-8601 cutoff: conversations whose"
                    " ``updated_at`` is earlier than this are skipped."
                ),
            },
            "region": {
                "type": "string",
                "description": (
                    "BSage region the seeds ingest into. Overrides binding"
                    " config's ``default_region`` (which itself defaults to"
                    " ``imported-claude``)."
                ),
            },
        },
        "additionalProperties": False,
    },
)
async def import_conversations(
    context: SkillContext,
    claude_binding_id: str | None = None,
    export_path: str | None = None,
    since: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """Read ``conversations.json`` and seed every conversation.

    Falls back to ``context.config['export_path']`` /
    ``context.config['since']`` / ``context.config['default_region']``
    when the corresponding kwarg is not supplied. Raises
    :class:`ValueError` (→ ``PluginRunError`` at the runner boundary)
    when no export path is resolvable or when the knowledge backend is
    missing.

    Returns a summary dict ``{conversations_count, messages_count,
    skipped, region}``.
    """
    resolved_path = export_path or context.config.get("export_path")
    if not resolved_path:
        raise ValueError(
            "claude.import_conversations: missing required 'export_path'"
            " (pass as arg or set on the binding config)"
        )

    knowledge = getattr(context, "knowledge", None)
    if knowledge is None:
        raise ValueError(
            "claude.import_conversations: SkillContext.knowledge is required"
            " but was not injected (worker bootstrap should wire the garden)"
        )

    resolved_binding = claude_binding_id or context.config.get("binding_id") or "default"
    resolved_since = since if since is not None else context.config.get("since")
    resolved_region = region or context.config.get("default_region") or "imported-claude"

    json_path = _resolve_export_file(str(resolved_path))
    if not json_path.is_file():
        raise ValueError(
            f"claude.import_conversations: conversations.json not found at {json_path}"
        )

    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"claude.import_conversations: failed to parse {json_path}: {exc}"
        ) from exc

    conversations, skipped = parse_export(payload, since=resolved_since)

    conversations_count = 0
    messages_count = 0
    for convo in conversations:
        try:
            markdown = render_markdown(convo)
        except Exception:  # noqa: BLE001 — soft-fail per convo
            logger.warning(
                "claude_render_failed",
                uuid=convo.uuid,
                exc_info=True,
            )
            skipped += 1
            continue

        seed_data: dict[str, Any] = {
            "title": convo.title,
            "content": markdown,
            "region": resolved_region,
            # Stable provenance — re-imports of the same conversation hit
            # the IngestCompiler content-hash dedup on the same key.
            "source_ref": f"claude://{resolved_binding}/{convo.uuid}",
            "frontmatter": render_frontmatter_only(convo),
        }

        try:
            await knowledge.write_seed("claude", seed_data)
        except Exception:  # noqa: BLE001 — soft-fail per conversation
            logger.warning(
                "claude_seed_write_failed",
                uuid=convo.uuid,
                exc_info=True,
            )
            skipped += 1
            continue

        conversations_count += 1
        messages_count += len(convo.messages)

    logger.info(
        AUDIT_EVENT_IMPORTED,
        binding_id=resolved_binding,
        region=resolved_region,
        conversations_count=conversations_count,
        messages_count=messages_count,
        skipped=skipped,
    )

    return {
        "conversations_count": conversations_count,
        "messages_count": messages_count,
        "skipped": skipped,
        "region": resolved_region,
    }


# ── setup ────────────────────────────────────────────────────────────────────


@p.setup
async def setup(cred_store: Any) -> dict[str, Any]:
    """Binding flow for the claude connector.

    Claude export has no API credentials — the "credential" payload is
    purely configuration:

    * ``CLAUDE_EXPORT_PATH`` (required) — absolute path to
      ``conversations.json`` or the directory containing it.
    * ``CLAUDE_SINCE`` (optional) — ISO-8601 timestamp; only
      conversations updated at or after this point are imported.
    * ``CLAUDE_DEFAULT_REGION`` (optional) — region label seeds ingest
      into when ``import_conversations`` is called without an explicit
      ``region`` (defaults to ``imported-claude`` if not set).

    Env-based ingestion keeps the path out of shell history / process
    args and stays non-interactive for CI / headless setup.
    """
    export_path = os.environ.get("CLAUDE_EXPORT_PATH")
    if not export_path:
        raise ValueError(
            "claude setup: set CLAUDE_EXPORT_PATH to the absolute path of"
            " conversations.json (or its containing directory) in the environment"
        )
    data: dict[str, Any] = {"export_path": export_path}
    since = os.environ.get("CLAUDE_SINCE")
    if since:
        data["since"] = since
    region = os.environ.get("CLAUDE_DEFAULT_REGION")
    if region:
        data["default_region"] = region
    await cred_store.store("claude", data)
    return data
