"""GardenWriter — composed public class.

Composes :class:`_WriterIOMixin`, :class:`_WriterMutationMixin`, and
:class:`_WriterToolHandlersMixin` plus the constructor / sync-notification
helpers. Extracted from the original monolithic ``writer_core.py`` during
Lift L1 (v8 §17.3).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer_core._io import _WriterIOMixin
from backend.knowledge.graph.writer_core._mutation import _WriterMutationMixin
from backend.knowledge.graph.writer_core._tombstone import _WriterTombstoneMixin
from backend.knowledge.graph.writer_core._tool_handlers import _WriterToolHandlersMixin

if TYPE_CHECKING:
    from backend.knowledge._internal.events import EventBus

    # TODO(bundle-k-integration): wire to plugin.audit
    AiosqliteAuditOutbox = Any
    from backend.knowledge.graph.sync import SyncManager
    from backend.knowledge.retrieval.ontology import OntologyRegistry

logger = structlog.get_logger(__name__)


class GardenWriter(
    _WriterIOMixin,
    _WriterMutationMixin,
    _WriterTombstoneMixin,
    _WriterToolHandlersMixin,
):
    """Writes seeds, garden notes, and action logs to the vault.

    Optionally notifies a SyncManager after each write so that
    registered backends (S3, Git, etc.) can sync the vault.

    Attributes:
        vault: The Vault instance for path resolution and file access.
    """

    def __init__(
        self,
        vault: Vault,
        sync_manager: SyncManager | None = None,
        event_bus: EventBus | None = None,
        ontology: OntologyRegistry | None = None,
        default_tenant_id: str | None = None,
        audit_outbox: AiosqliteAuditOutbox | None = None,
    ) -> None:
        self._vault = vault
        self._sync_manager = sync_manager
        self._event_bus = event_bus
        self._ontology = ontology
        # Phase 0 P0.5 — fallback tenant id used when GardenNote.tenant_id is
        # None. Lets cron / migration writes still satisfy the tenant column
        # without dragging a principal through every internal call site.
        self._default_tenant_id = default_tenant_id
        # Phase Audit Batch 2 — optional outbox; when wired, the writer emits
        # ``sage.vault.file_modified`` events after every successful vault
        # write. ``None`` keeps test fixtures simple (no audit infra needed).
        self._audit_outbox = audit_outbox
        self._log_lock = asyncio.Lock()
        self._garden_lock = asyncio.Lock()
        self._seed_lock = asyncio.Lock()

    async def _notify_sync(self, event_type_str: str, path: Path, source: str) -> None:
        """Notify sync manager of a write event, if configured."""
        if self._sync_manager is None:
            return
        from backend.knowledge.graph.sync import WriteEvent, WriteEventType

        event = WriteEvent(
            event_type=WriteEventType(event_type_str),
            path=path,
            source=source,
        )
        await self._sync_manager.notify(event)

    async def _emit_vault_modified(
        self,
        *,
        path: Path,
        operation: str,
        source: str,
        note_type: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        """Best-effort emit of ``sage.vault.file_modified`` after a write.

        Failures here NEVER raise — audit observability must not break a
        successful vault write. The handler logs and continues.
        """
        # TODO(bundle-k-integration): rewire to plugin.audit.safe_emit.
        del path, operation, source, note_type, tenant_id  # noqa: ERA001

    @staticmethod
    def _find_dedup_path(directory: Path, slug: str) -> Path:
        """Find the next available deduplicated filename.

        Searches for slug_001.md, slug_002.md, etc. until a free name is found.
        """
        counter = 1
        max_attempts = 9999
        while counter <= max_attempts:
            candidate = directory / f"{slug}_{counter:03d}.md"
            if not candidate.exists():
                return candidate
            counter += 1
        # Fallback: use timestamp-based name to guarantee uniqueness
        ts = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S%f")
        return directory / f"{slug}_{ts}.md"
