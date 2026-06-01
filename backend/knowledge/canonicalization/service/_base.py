"""Shared base for canonicalization service mixins (typing-only).

The mixin decomposition per v8 §17.4 splits the apply pipeline across files.
Mixin methods cross-reference each other (e.g. ``_proposal_lifecycle.expire_stale``
calls ``self._invalidate_index`` implemented in ``_apply_pipeline``). To keep
mypy --strict happy without coupling the mixins via inheritance order, we
declare a single ``_ServiceBase`` with type-only stubs for every cross-mixin
attribute and method. Each mixin inherits from this base; the concrete
``CanonicalizationService`` overrides the stubs with real implementations.

This is a Python-stdlib-recommended pattern for type-checked mixin hierarchies
(PEP 544 Protocols + MRO). The runtime stubs all ``raise NotImplementedError``
so that an accidental MRO inversion fails loudly instead of silently no-op.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from backend.knowledge.canonicalization import models

if TYPE_CHECKING:  # pragma: no cover
    from backend.knowledge._internal.events import EventBus
    from backend.knowledge.canonicalization.decisions import DecisionMemory
    from backend.knowledge.canonicalization.index import CanonicalizationIndex
    from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
    from backend.knowledge.canonicalization.policies import PolicyResolver
    from backend.knowledge.canonicalization.resolver import TagResolver
    from backend.knowledge.canonicalization.scoring import CanonicalizationScorer
    from backend.knowledge.canonicalization.store import NoteStore


class _ServiceBase:
    """Typing-only base for all canonicalization service mixins.

    Holds the attribute schema set by ``CanonicalizationService.__init__``
    plus stubs for every cross-mixin method so mypy --strict can resolve
    references without depending on inheritance order.

    All stubs raise NotImplementedError at runtime — they are placeholders
    for the concrete implementations supplied by the mixin that owns them.
    """

    # ---- attributes (set by CanonicalizationService.__init__) ----
    _store: NoteStore
    _lock: AsyncIOMutationLock
    _index: CanonicalizationIndex | None
    _resolver: TagResolver | None
    _decisions: DecisionMemory | None
    _policies: PolicyResolver | None
    _clock: Callable[[], datetime]
    _event_bus: EventBus | None
    _safe_mode: Callable[[], bool]
    _scorer: CanonicalizationScorer | None

    # ---- _apply_pipeline.py ----
    async def apply_action(self, action_path: str, *, actor: str) -> models.ApplyResult:
        raise NotImplementedError

    async def _invalidate_index(self, paths_: list[str]) -> None:
        raise NotImplementedError

    async def _persist_blocked(
        self,
        entry: models.ActionEntry,
        validation: models.ValidationResult,
        previous_status: str = "draft",
    ) -> models.ApplyResult:
        raise NotImplementedError

    async def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    async def _emit_action_status(self, entry: models.ActionEntry, previous_status: str) -> None:
        raise NotImplementedError

    async def _emit_action_applied(self, entry: models.ActionEntry) -> None:
        raise NotImplementedError

    async def _emit_kind_specific_applied(self, entry: models.ActionEntry) -> None:
        raise NotImplementedError

    # ---- _validators.py ----
    async def _validate(self, entry: models.ActionEntry) -> models.ValidationResult:
        raise NotImplementedError

    # ---- _effects.py ----
    async def _persist_effects(self, entry: models.ActionEntry) -> list[str]:
        raise NotImplementedError

    # ---- _safe_mode.py ----
    async def _safe_mode_permits_auto_apply(self, entry: models.ActionEntry) -> bool:
        raise NotImplementedError

    async def _handle_safe_mode(
        self,
        entry: models.ActionEntry,
        validation: models.ValidationResult,
        previous_status: str,
        actor: str,
    ) -> models.ApplyResult:
        raise NotImplementedError
