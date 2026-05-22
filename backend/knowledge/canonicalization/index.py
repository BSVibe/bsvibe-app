"""CanonicalizationIndex — derived lookup over vault notes (Handoff §10).

Slice 2 ships only ``InMemoryCanonicalizationIndex`` (self-host v1). SaaS
``PostgresCanonicalizationIndex`` is deferred to v1.x. Pattern follows
existing ``GraphBackend`` ABC + ``VaultBackend`` (NetworkX) duality at
``bsage/garden/graph_backend.py`` (Class_Diagram §10.2).

Per §0.1 the vault is SoT. The index is rebuildable from vault markdown
alone; cold start scans ``StorageBackend.list_files()`` and reads each note.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

import structlog

from backend.knowledge.canonicalization import models, paths
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.markdown_utils import extract_frontmatter
from backend.knowledge.graph.storage import StorageBackend

logger = structlog.get_logger(__name__)


class CanonicalizationIndex(ABC):
    """Derived lookup ABC (Handoff §10).

    All canonicalization status queries from API/MCP/frontend MUST go
    through this interface; the underlying index MUST be rebuildable from
    vault markdown alone.
    """

    @abstractmethod
    async def initialize(self, storage: StorageBackend) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # Concept lookup
    @abstractmethod
    async def get_active_concept(self, concept_id: str) -> models.ConceptEntry | None: ...

    @abstractmethod
    async def list_active_concepts(self) -> list[models.ConceptEntry]: ...

    @abstractmethod
    async def find_concepts_by_alias(self, alias: str) -> list[models.ConceptEntry]: ...

    @abstractmethod
    async def get_tombstone(self, old_id: str) -> models.TombstoneEntry | None: ...

    @abstractmethod
    async def get_deprecated(self, concept_id: str) -> models.DeprecatedEntry | None: ...

    # Action queue
    @abstractmethod
    async def list_actions(
        self, *, status: str | None = None, kind: str | None = None
    ) -> list[models.ActionEntry]: ...

    @abstractmethod
    async def list_proposals(
        self, *, status: str | None = None, kind: str | None = None
    ) -> list[models.ProposalEntry]: ...

    @abstractmethod
    async def find_pending_concept_draft(
        self, normalized_tag: str
    ) -> models.ActionEntry | None: ...

    # Decision / policy lookup
    @abstractmethod
    async def list_decisions(
        self, *, kind: str | None = None, status: str | None = None
    ) -> list[models.DecisionEntry]: ...

    @abstractmethod
    async def list_policies(
        self, *, kind: str | None = None, status: str | None = None
    ) -> list[models.PolicyEntry]: ...

    # Lifecycle
    @abstractmethod
    async def invalidate(self, path: str) -> None: ...

    @abstractmethod
    async def rebuild_from_vault(self, storage: StorageBackend) -> None: ...


# Per Handoff §6 — `applied`, `rejected`, `expired`, `superseded`, `failed`
# are terminal. `blocked` is recoverable (a redraft can re-apply).
_PENDING_ACTION_STATUSES: frozenset[str] = frozenset({"draft", "pending_approval"})


class InMemoryCanonicalizationIndex(CanonicalizationIndex):
    """In-process dict-of-dicts index (self-host v1)."""

    def __init__(self) -> None:
        self._storage: StorageBackend | None = None
        self._concepts: dict[str, models.ConceptEntry] = {}
        self._proposals: dict[str, models.ProposalEntry] = {}
        # alias_lower -> set of concept ids
        self._aliases: dict[str, set[str]] = {}
        self._tombstones: dict[str, models.TombstoneEntry] = {}
        self._deprecated: dict[str, models.DeprecatedEntry] = {}
        self._actions: dict[str, models.ActionEntry] = {}
        self._decisions: dict[str, models.DecisionEntry] = {}
        self._policies: dict[str, models.PolicyEntry] = {}

    async def initialize(self, storage: StorageBackend) -> None:
        self._storage = storage
        await self.rebuild_from_vault(storage)

    async def close(self) -> None:
        self._concepts.clear()
        self._aliases.clear()
        self._tombstones.clear()
        self._deprecated.clear()
        self._actions.clear()
        self._proposals.clear()
        self._decisions.clear()
        self._policies.clear()
        self._storage = None

    # --------------------------------------------------------------- queries

    async def get_active_concept(self, concept_id: str) -> models.ConceptEntry | None:
        return self._concepts.get(concept_id)

    async def list_active_concepts(self) -> list[models.ConceptEntry]:
        return [self._concepts[c] for c in sorted(self._concepts)]

    async def find_concepts_by_alias(self, alias: str) -> list[models.ConceptEntry]:
        ids = self._aliases.get(alias.casefold(), set())
        return [self._concepts[c] for c in sorted(ids) if c in self._concepts]

    async def get_tombstone(self, old_id: str) -> models.TombstoneEntry | None:
        return self._tombstones.get(old_id)

    async def get_deprecated(self, concept_id: str) -> models.DeprecatedEntry | None:
        return self._deprecated.get(concept_id)

    async def list_actions(
        self, *, status: str | None = None, kind: str | None = None
    ) -> list[models.ActionEntry]:
        out = []
        for entry in self._actions.values():
            if status is not None and entry.status != status:
                continue
            if kind is not None and entry.kind != kind:
                continue
            out.append(entry)
        return out

    async def find_pending_concept_draft(self, normalized_tag: str) -> models.ActionEntry | None:
        for entry in self._actions.values():
            if entry.kind != "create-concept":
                continue
            if entry.status not in _PENDING_ACTION_STATUSES:
                continue
            if entry.params.get("concept") == normalized_tag:
                return entry
        return None

    async def list_proposals(
        self, *, status: str | None = None, kind: str | None = None
    ) -> list[models.ProposalEntry]:
        out: list[models.ProposalEntry] = []
        for entry in self._proposals.values():
            if status is not None and entry.status != status:
                continue
            if kind is not None and entry.kind != kind:
                continue
            out.append(entry)
        return out

    async def list_decisions(
        self, *, kind: str | None = None, status: str | None = None
    ) -> list[models.DecisionEntry]:
        out: list[models.DecisionEntry] = []
        for entry in self._decisions.values():
            if kind is not None and entry.kind != kind:
                continue
            if status is not None and entry.status != status:
                continue
            out.append(entry)
        return out

    async def list_policies(
        self, *, kind: str | None = None, status: str | None = None
    ) -> list[models.PolicyEntry]:
        out: list[models.PolicyEntry] = []
        for entry in self._policies.values():
            if kind is not None and entry.kind != kind:
                continue
            if status is not None and entry.status != status:
                continue
            out.append(entry)
        return out

    # ----------------------------------------------------------- mutation

    async def invalidate(self, path: str) -> None:
        if self._storage is None:
            msg = "index not initialized — call initialize() first"
            raise RuntimeError(msg)
        await self._reload_path(self._storage, path)

    async def rebuild_from_vault(self, storage: StorageBackend) -> None:
        self._storage = storage
        self._concepts.clear()
        self._aliases.clear()
        self._tombstones.clear()
        self._deprecated.clear()
        self._actions.clear()
        self._proposals.clear()
        self._decisions.clear()
        self._policies.clear()

        store = NoteStore(storage)
        for path in await storage.list_files("concepts/active"):
            concept_id = _stem(path)
            entry = await store.read_concept(concept_id)
            if entry is not None:
                self._add_concept(entry)
        for path in await storage.list_files("concepts/merged"):
            ts = await _read_tombstone(storage, path)
            if ts is not None:
                self._tombstones[ts.old_id] = ts
        for path in await storage.list_files("concepts/deprecated"):
            dep = await _read_deprecated(storage, path)
            if dep is not None:
                self._deprecated[dep.concept_id] = dep
        # ``actions/`` is shared with the legacy GardenWriter agent action log
        # (``actions/{YYYY-MM-DD}.md`` and ``actions/input-log/...``) so we
        # MUST filter to canon paths only — kind-subdir under a known action
        # kind. Same defensive filter for proposals/ and decisions/.
        for path in await storage.list_files("actions"):
            if not _is_canon_action_path(path):
                continue
            action = await store.read_action(path)
            if action is not None:
                self._actions[path] = action
        for path in await storage.list_files("proposals"):
            if not _is_canon_proposal_path(path):
                continue
            proposal = await store.read_proposal(path)
            if proposal is not None:
                self._proposals[path] = proposal
        # decisions/<kind>/... AND decisions/policy/<kind>/<profile>.md
        for path in await storage.list_files("decisions"):
            if _is_canon_policy_path(path):
                policy = await store.read_policy(path)
                if policy is not None:
                    self._policies[path] = policy
            elif _is_canon_decision_path(path):
                decision = await store.read_decision(path)
                if decision is not None:
                    self._decisions[path] = decision

    # ----------------------------------------------------------- helpers

    async def _reload_path(self, storage: StorageBackend, path: str) -> None:
        store = NoteStore(storage)
        exists = await storage.exists(path)
        if path.startswith("concepts/active/"):
            concept_id = _stem(path)
            self._remove_concept(concept_id)
            if exists:
                entry = await store.read_concept(concept_id)
                if entry is not None:
                    self._add_concept(entry)
        elif path.startswith("concepts/merged/"):
            old_id = _stem(path)
            self._tombstones.pop(old_id, None)
            if exists:
                ts = await _read_tombstone(storage, path)
                if ts is not None:
                    self._tombstones[ts.old_id] = ts
        elif path.startswith("concepts/deprecated/"):
            concept_id = _stem(path)
            self._deprecated.pop(concept_id, None)
            if exists:
                dep = await _read_deprecated(storage, path)
                if dep is not None:
                    self._deprecated[dep.concept_id] = dep
        elif path.startswith("proposals/"):
            if not _is_canon_proposal_path(path):
                return
            self._proposals.pop(path, None)
            if exists:
                proposal = await store.read_proposal(path)
                if proposal is not None:
                    self._proposals[path] = proposal
        elif path.startswith("decisions/policy/"):
            if not _is_canon_policy_path(path):
                return
            self._policies.pop(path, None)
            if exists:
                policy = await store.read_policy(path)
                if policy is not None:
                    self._policies[path] = policy
        elif path.startswith("decisions/"):
            if not _is_canon_decision_path(path):
                return
            self._decisions.pop(path, None)
            if exists:
                decision = await store.read_decision(path)
                if decision is not None:
                    self._decisions[path] = decision
        elif path.startswith("actions/"):
            if not _is_canon_action_path(path):
                # Legacy GardenWriter action log (actions/{YYYY-MM-DD}.md
                # or actions/input-log/...) — never indexed, never raised.
                return
            self._actions.pop(path, None)
            if exists:
                action = await store.read_action(path)
                if action is not None:
                    self._actions[path] = action
        else:
            logger.debug("canon_index_invalidate_ignored", path=path)

    def _add_concept(self, entry: models.ConceptEntry) -> None:
        self._concepts[entry.concept_id] = entry
        for alias in entry.aliases:
            self._aliases.setdefault(alias.casefold(), set()).add(entry.concept_id)

    def _remove_concept(self, concept_id: str) -> None:
        self._concepts.pop(concept_id, None)
        empty: list[str] = []
        for alias_key, ids in self._aliases.items():
            ids.discard(concept_id)
            if not ids:
                empty.append(alias_key)
        for k in empty:
            del self._aliases[k]


def _stem(path: str) -> str:
    name = PurePosixPath(path).name
    return name[:-3] if name.endswith(".md") else name


async def _read_tombstone(storage: StorageBackend, path: str) -> models.TombstoneEntry | None:
    text = await storage.read(path)
    fm = extract_frontmatter(text)
    merged_into = fm.get("merged_into")
    if not isinstance(merged_into, str):
        return None
    return models.TombstoneEntry(
        old_id=_stem(path),
        path=path,
        merged_into=merged_into,
        merged_at=_parse_iso(fm.get("merged_at")) or datetime.min,
        source_action=fm.get("source_action"),
    )


async def _read_deprecated(storage: StorageBackend, path: str) -> models.DeprecatedEntry | None:
    text = await storage.read(path)
    fm = extract_frontmatter(text)
    return models.DeprecatedEntry(
        concept_id=_stem(path),
        path=path,
        deprecated_at=_parse_iso(fm.get("deprecated_at")) or datetime.min,
        replacement=fm.get("replacement"),
        reason=fm.get("reason"),
        source_action=fm.get("source_action"),
    )


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


# --------------------------------------------------------- canon path predicates

# These filters MUST be applied before calling NoteStore.read_*.
# ``actions/`` is shared with the legacy GardenWriter agent action log
# (``actions/{YYYY-MM-DD}.md`` and ``actions/input-log/{YYYY-MM-DD}.md``);
# slices 1-4 missed this collision and the canon index crashed at boot
# whenever the vault contained any pre-existing agent log. Future canon
# top-level dirs that share a legacy root MUST go through these predicates.

_KNOWN_ACTION_KINDS: frozenset[str] = frozenset(models.ACTION_KINDS)


def _is_canon_action_path(path: str) -> bool:
    """``actions/<kind>/...md`` where ``<kind>`` is a known action kind."""
    parts = PurePosixPath(path).parts
    if len(parts) < 3 or parts[0] != "actions":
        return False
    if parts[1] not in _KNOWN_ACTION_KINDS:
        return False
    if not path.endswith(".md"):
        return False
    # ``create-decision`` has an extra ``<decision-kind>/`` segment per §7.8.
    return not (
        parts[1] == "create-decision" and (len(parts) < 4 or parts[2] not in paths.DECISION_KINDS)
    )


def _is_canon_proposal_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        len(parts) >= 3
        and parts[0] == "proposals"
        and parts[1] in paths.PROPOSAL_KINDS
        and path.endswith(".md")
    )


def _is_canon_decision_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        len(parts) >= 3
        and parts[0] == "decisions"
        and parts[1] in paths.DECISION_KINDS
        and path.endswith(".md")
    )


def _is_canon_policy_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        len(parts) >= 4
        and parts[0] == "decisions"
        and parts[1] == "policy"
        and parts[2] in paths.POLICY_KINDS
        and path.endswith(".md")
    )
