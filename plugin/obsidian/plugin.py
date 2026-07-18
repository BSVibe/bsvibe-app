"""Obsidian connector — capability registrations (Workflow §6 #4, Lift Q3).

The :class:`backend.extensions.plugin.PluginLoader` imports this file and
picks up the module-level ``p`` (a :class:`PluginBuilder`). The
``import_vault`` action walks the configured local Obsidian vault, parses
frontmatter, and submits each markdown note as a seed via the restricted
garden surface (``context.knowledge.write_seed``) so ``IngestCompiler``
classifies it on the next compile pass.

No outbound dispatch / compensate / webhook intake — Obsidian import is a
one-way inbound knowledge ingest.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

from backend.extensions.plugin.context import SkillContext
from bsvibe_sdk import plugin
from plugin.obsidian.client import VaultScanner
from plugin.obsidian.parser import parse_note

logger = structlog.get_logger(__name__)

# Audit / structured-log event name emitted on every successful import. The
# string is the spec-mandated identifier (Q3-Obsidian) so log searches +
# future audit relays can route on it deterministically.
AUDIT_EVENT_IMPORTED = "audit.knowledge.imported.obsidian"

p = plugin(
    name="obsidian",
    version="0.1.0",
    description="Obsidian vault knowledge import — scans a local vault and seeds BSage.",
    author="BSVibe",
    # The vault sits on the founder's local machine; no cloud / regional
    # data residency boundary applies — local-only data.
    data_jurisdiction="local",
    # No external API credentials; the binding config carries vault_path.
    credentials=[],
)


# ── actions (agent-loop tools) ─────────────────────────────────────────────


@p.action(
    name="import_vault",
    mcp_exposed=True,
    import_trigger=True,
    input_schema={
        "type": "object",
        "required": [],
        "properties": {
            "vault_path": {
                "type": "string",
                "description": (
                    "Absolute path to the Obsidian vault root. Falls back to"
                    " the binding config's ``vault_path`` when omitted."
                ),
            },
            "exclude_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional fnmatch globs to skip (default: .obsidian/**, Templates/**)."
                ),
            },
            "region": {
                "type": "string",
                "description": (
                    "BSage region the seeds ingest into. Overrides binding"
                    " config's ``default_region`` (which itself defaults to"
                    " ``imported``)."
                ),
            },
        },
        "additionalProperties": False,
    },
)
async def import_vault(
    context: SkillContext,
    vault_path: str | None = None,
    exclude_patterns: list[str] | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """Walk the founder's Obsidian vault and seed every markdown note.

    Falls back to ``context.config['vault_path']`` /
    ``context.config['exclude_patterns']`` /
    ``context.config['default_region']`` when the corresponding kwarg is
    not supplied. Raises :class:`ValueError` (→ ``PluginRunError`` at the
    runner boundary) when no vault path is resolvable or when the knowledge
    backend is missing.

    Returns a summary dict ``{notes_count, scanned_count, skipped_count}``.
    """
    resolved_path = vault_path or context.config.get("vault_path")
    if not resolved_path:
        raise ValueError(
            "obsidian.import_vault: missing required 'vault_path'"
            " (pass as arg or set on the binding config)"
        )

    knowledge = getattr(context, "knowledge", None)
    if knowledge is None:
        raise ValueError(
            "obsidian.import_vault: SkillContext.knowledge is required but"
            " was not injected (worker bootstrap should wire the garden)"
        )

    resolved_excludes = exclude_patterns
    if resolved_excludes is None:
        resolved_excludes = context.config.get("exclude_patterns")  # may be None
    resolved_region = region or context.config.get("default_region") or "imported"

    scanner = VaultScanner(Path(resolved_path), exclude_patterns=resolved_excludes)

    notes_count = 0
    scanned_count = 0
    skipped_count = 0
    for note in scanner.scan():
        scanned_count += 1
        try:
            metadata, body = parse_note(note.text)
        except Exception:  # noqa: BLE001 — defensive: never let one bad note kill the batch
            logger.warning(
                "obsidian_note_parse_failed",
                relative_path=note.relative_path,
                exc_info=True,
            )
            skipped_count += 1
            continue

        # Default title to the filename stem so seeds always have a title
        # even when frontmatter is absent (which is the common case in
        # most personal Obsidian vaults).
        title = metadata.get("title") or Path(note.relative_path).stem
        tags = metadata.get("tags")

        seed_data: dict[str, Any] = {
            "title": title,
            "content": body,
            "region": resolved_region,
            # Stable provenance suffix so re-imports of the same note hit
            # IngestCompiler's content-hash dedup path on the same key.
            "source_ref": f"obsidian://{note.relative_path}",
        }
        if tags is not None:
            seed_data["tags"] = tags
        # Carry frontmatter through under a stable key so downstream
        # canonicalization can read original fields without re-parsing.
        if metadata:
            seed_data["frontmatter"] = metadata

        try:
            await knowledge.write_seed("obsidian", seed_data)
        except Exception:  # noqa: BLE001 — soft-fail per-note; full batch must still finish
            logger.warning(
                "obsidian_seed_write_failed",
                relative_path=note.relative_path,
                exc_info=True,
            )
            skipped_count += 1
            continue

        notes_count += 1

    logger.info(
        AUDIT_EVENT_IMPORTED,
        vault_path=str(resolved_path),
        region=resolved_region,
        notes_count=notes_count,
        scanned_count=scanned_count,
        skipped_count=skipped_count,
    )

    return {
        "notes_count": notes_count,
        "scanned_count": scanned_count,
        "skipped_count": skipped_count,
        "region": resolved_region,
    }


# ── setup ────────────────────────────────────────────────────────────────────


@p.setup
async def setup(cred_store: Any) -> dict[str, Any]:
    """Binding flow for the obsidian connector.

    Obsidian has no API credentials — the "credential" payload is purely
    configuration:

    * ``OBSIDIAN_VAULT_PATH`` (required) — absolute path to the vault root.
    * ``OBSIDIAN_EXCLUDE_PATTERNS`` (optional) — comma-separated fnmatch
      globs that override the defaults (``.obsidian/**``, ``Templates/**``).
    * ``OBSIDIAN_DEFAULT_REGION`` (optional) — region label seeds ingest
      into when ``import_vault`` is called without an explicit ``region``.

    Env-based ingestion keeps the path out of shell history / process args
    and stays non-interactive for CI / headless setup.
    """
    vault_path = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not vault_path:
        raise ValueError(
            "obsidian setup: set OBSIDIAN_VAULT_PATH to the absolute vault"
            " root path in the environment"
        )
    data: dict[str, Any] = {"vault_path": vault_path}
    excludes_raw = os.environ.get("OBSIDIAN_EXCLUDE_PATTERNS")
    if excludes_raw:
        data["exclude_patterns"] = [p.strip() for p in excludes_raw.split(",") if p.strip()]
    region = os.environ.get("OBSIDIAN_DEFAULT_REGION")
    if region:
        data["default_region"] = region
    await cred_store.store("obsidian", data)
    return data
