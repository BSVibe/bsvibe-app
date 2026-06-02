"""_WriterTombstoneMixin — ontology retraction tombstone + restore (Lift M3a).

The ontology inspect/correct UX retracts a node by **marking** it, not by
deleting it: a YAML frontmatter ``retracted_at`` (plus ``retracted_by`` and
optional ``retraction_reason``) flips the
:class:`~backend.knowledge.retrieval.resolved_decisions_retriever.ResolvedDecisionsRetriever`'s
skip predicate without losing the provenance the D5 ratchet test asserts.

The two operations here are the writer-side of that flow. The application
service :class:`~backend.knowledge.application.retraction_service.RetractionService`
owns the timing (30s undo window, DB-backed) and the audit emit; this
mixin owns the on-disk mutation.

Split out of :mod:`._mutation` to keep that file under the 350-LOC cap the
Lift L1 decomposition tests enforce.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from backend.knowledge._internal.events import emit_event
from backend.knowledge.graph.note import build_frontmatter
from backend.knowledge.graph.vault import Vault

if TYPE_CHECKING:
    from backend.knowledge._internal.events import EventBus

    AiosqliteAuditOutbox = Any

logger = structlog.get_logger(__name__)


class _WriterTombstoneMixin:
    """Frontmatter tombstone + restore operations for ontology retraction."""

    _vault: Vault
    _event_bus: EventBus | None
    _audit_outbox: AiosqliteAuditOutbox | None
    _garden_lock: asyncio.Lock

    async def _notify_sync(  # pragma: no cover - in GardenWriter
        self, event_type_str: str, path: Path, source: str
    ) -> None: ...

    async def _emit_vault_modified(  # pragma: no cover - in GardenWriter
        self,
        *,
        path: Path,
        operation: str,
        source: str,
        note_type: str | None = None,
        tenant_id: str | None = None,
    ) -> None: ...

    async def _set_frontmatter_field(  # pragma: no cover - in _mutation
        self, path: Path, key: str, value: Any
    ) -> None: ...

    async def tombstone_note(
        self,
        path: str,
        *,
        retracted_at: str,
        retracted_by: str,
        retraction_reason: str | None = None,
    ) -> Path:
        """Mark a vault note as retracted via frontmatter — never delete the file.

        Lift M3a. Adds ``retracted_at`` (+ ``retracted_by`` + optional
        ``retraction_reason``) to the note's YAML frontmatter; the body is
        untouched. The retriever's ``retracted_at`` skip predicate flips
        the note from surfaced to hidden without losing provenance the
        design (§1.3) and Workflow Backend §3 require.

        Reuses :meth:`_set_frontmatter_field` so the existing atomic
        single-field mutation (lock + write under ``_garden_lock``) carries
        the retraction — every other writer that calls into the vault is
        already serialized with us.

        Idempotence: setting ``retracted_at`` twice with the same value is
        a no-op for the retriever (the predicate is "is the key present"),
        and the service-layer caller short-circuits before re-writing.

        Returns the resolved absolute path the tombstone was written to.
        Raises ``FileNotFoundError`` when the note path doesn't exist —
        the service checks existence first so the REST handler 404s
        rather than 500s.
        """
        resolved = self._vault.resolve_path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Note not found: {path}")
        await self._set_frontmatter_field(resolved, "retracted_at", retracted_at)
        await self._set_frontmatter_field(resolved, "retracted_by", retracted_by)
        if retraction_reason is not None:
            await self._set_frontmatter_field(resolved, "retraction_reason", retraction_reason)
        rel_path = str(resolved.relative_to(self._vault.root))
        await emit_event(
            self._event_bus,
            "NOTE_UPDATED",
            {"path": rel_path, "field": "retracted_at", "new_value": retracted_at},
        )
        await self._notify_sync("garden", resolved, "update")
        await self._emit_vault_modified(
            path=resolved,
            operation="note_tombstoned",
            source="ontology_retraction",
        )
        logger.info("note_tombstoned", path=rel_path, retracted_by=retracted_by)
        return resolved

    async def restore_note_from_tombstone(self, path: str) -> Path:
        """Undo a tombstone — clear ``retracted_at`` / ``retracted_by`` / reason.

        Mirrors :meth:`tombstone_note`: the body is untouched and the file
        was never moved, so removing the three frontmatter keys is the
        full restore. Idempotent — clearing a key that isn't present is a
        no-op.

        Raises ``FileNotFoundError`` for an unknown path.
        """
        resolved = self._vault.resolve_path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Note not found: {path}")
        for key in ("retracted_at", "retracted_by", "retraction_reason"):
            await self._clear_frontmatter_field(resolved, key)
        rel_path = str(resolved.relative_to(self._vault.root))
        await emit_event(
            self._event_bus,
            "NOTE_UPDATED",
            {"path": rel_path, "field": "retracted_at", "new_value": None},
        )
        await self._notify_sync("garden", resolved, "update")
        await self._emit_vault_modified(
            path=resolved,
            operation="note_restored",
            source="ontology_retraction",
        )
        logger.info("note_restored", path=rel_path)
        return resolved

    async def _clear_frontmatter_field(self, path: Path, key: str) -> None:
        """Remove a single frontmatter field if present; no-op when absent."""
        async with self._garden_lock:
            text = await asyncio.to_thread(path.read_text, "utf-8")
            if not text.startswith("---\n"):
                return
            closing = text.find("\n---\n", 4)
            if closing == -1:
                return
            try:
                fm = yaml.safe_load(text[4:closing]) or {}
            except yaml.YAMLError:
                return
            if not isinstance(fm, dict) or key not in fm:
                return
            fm.pop(key, None)
            new_text = build_frontmatter(fm) + text[closing + 5 :]
            await asyncio.to_thread(path.write_text, new_text, encoding="utf-8")


__all__ = ["_WriterTombstoneMixin"]
