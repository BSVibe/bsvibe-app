"""OntologyRegistry — manages the knowledge graph schema (v2.2)."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# v2.2 Default Ontology
# ---------------------------------------------------------------------------

_DEFAULT_ONTOLOGY: dict[str, Any] = {
    "schema_version": 4,
    "updated_at": "",
    # NOTE: ``entity_types`` was removed in schema v4 (dynamic-ontology
    # refactor). Identity comes from tags + entities + community, not a
    # static enum. ``relation_types`` and ``evolution_config`` stay because
    # the graph still uses typed edges; types of NODES are now free-form.
    "relation_types": {
        "related_to": {
            "domain": "*",
            "range": "*",
            "inverse": "related_to",
            "default_weight": 0.5,
            "description": "타입 미분류 임시 관계",
        },
        "references": {
            "domain": "*",
            "range": "*",
            "inverse": "referenced_by",
            "default_weight": 0.3,
            "description": "본문 wikilink 참조",
        },
        "tagged_with": {
            "domain": "*",
            "range": "tag",
            "inverse": "tag_of",
            "default_weight": 0.5,
            "description": "태그 분류",
        },
        "created_by": {
            "domain": "*",
            "range": "source",
            "inverse": "created",
            "default_weight": 0.3,
            "description": "소스에 의해 생성됨",
        },
        "part_of": {
            "domain": "*",
            "range": ["project", "organization"],
            "inverse": "has_member",
            "default_weight": 0.8,
            "description": "소속 관계",
        },
        "uses": {
            "domain": "person",
            "range": ["tool", "concept"],
            "inverse": "used_by",
            "default_weight": 0.9,
            "description": "도구/기술 사용",
        },
        "depends_on": {
            "domain": ["task", "project"],
            "range": ["task", "project"],
            "inverse": "depended_by",
            "default_weight": 0.8,
            "description": "의존 관계",
        },
        "assigned_to": {
            "domain": "task",
            "range": "person",
            "inverse": "assigned_tasks",
            "default_weight": 1.0,
            "description": "담당자 배정",
        },
        "attends": {
            "domain": "person",
            "range": "event",
            "inverse": "attended_by",
            "default_weight": 1.0,
            "description": "이벤트 참석",
        },
        "attendees": {
            "domain": "event",
            "range": "person",
            "inverse": "attends",
            "default_weight": 1.0,
            "description": "이벤트 참석자 (frontmatter key)",
        },
        "belongs_to": {
            "domain": "*",
            "range": ["project", "organization"],
            "inverse": "has_member",
            "default_weight": 0.8,
            "description": "소속",
        },
        "works_on": {
            "domain": "person",
            "range": "project",
            "inverse": "worked_on_by",
            "default_weight": 1.0,
            "description": "프로젝트 참여",
        },
        "mentions": {
            "domain": "*",
            "range": "*",
            "inverse": "mentioned_by",
            "default_weight": 0.1,
            "description": "맥락적 언급",
        },
        "supersedes": {
            "domain": "fact",
            "range": "fact",
            "inverse": "superseded_by",
            "default_weight": 1.0,
            "description": "이전 사실 대체",
        },
        "prefers": {
            "domain": "person",
            "range": "*",
            "inverse": "preferred_by",
            "default_weight": 0.7,
            "description": "선호 관계",
        },
    },
    "evolution_config": {
        "create_threshold": 5,
        "merge_jaccard_threshold": 0.7,
        "deprecate_days": 90,
        "deprecate_min_confidence": 0.3,
        "promotion_frequency_ratio": 2.0,
        "edge_promotion_min_mentions": 3,
        "edge_decay_days": 90,
    },
}


class OntologyRegistry:
    """Manages the ontology schema for the knowledge graph.

    The schema is stored as YAML at ``vault/.bsage/ontology.yaml``.
    If the file does not exist, a default ontology is created automatically.
    """

    def __init__(self, ontology_path: Path) -> None:
        self._path = ontology_path
        self._data: dict[str, Any] = {}

    async def load(self) -> None:
        """Load the ontology from disk, or create defaults if missing."""

        def _read() -> dict[str, Any] | None:
            if self._path.exists():
                with open(self._path) as f:
                    return yaml.safe_load(f)
            return None

        loaded = await asyncio.to_thread(_read)
        if loaded is not None:
            self._data = loaded if isinstance(loaded, dict) else copy.deepcopy(_DEFAULT_ONTOLOGY)
            logger.info("ontology_loaded", path=str(self._path))
        else:
            self._data = copy.deepcopy(_DEFAULT_ONTOLOGY)
            await self.save()
            logger.info("ontology_created_default", path=str(self._path))

    async def save(self) -> None:
        """Persist the ontology to disk."""
        data = copy.deepcopy(self._data)

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        await asyncio.to_thread(_write)

    # ------------------------------------------------------------------
    # Relationship types
    # ------------------------------------------------------------------

    def get_relation_types(self) -> dict[str, dict[str, Any]]:
        """Return all relation types."""
        return dict(self._data.get("relation_types", {}))

    # Keep old name as alias for compatibility during transition
    get_relationship_types = get_relation_types

    def is_valid_relationship_type(self, rel_type: str) -> bool:
        """Check if a relationship type exists in the ontology."""
        return rel_type in self._data.get("relation_types", {})

    async def add_relationship_type(
        self,
        name: str,
        description: str,
        *,
        domain: str | list[str] = "*",
        range_: str | list[str] = "*",
        inverse: str | None = None,
        default_weight: float = 0.5,
    ) -> bool:
        """Add a new relationship type. Returns True if added, False if already exists."""
        types = self._data.setdefault("relation_types", {})
        if name in types:
            return False
        entry: dict[str, Any] = {
            "domain": domain,
            "range": range_,
            "default_weight": default_weight,
            "description": description,
        }
        if inverse:
            entry["inverse"] = inverse
        types[name] = entry
        await self.save()
        logger.info("ontology_relationship_type_added", name=name)
        return True

    def get_relation_weight(self, rel_type: str) -> float:
        """Return the default weight for a relation type."""
        types = self._data.get("relation_types", {})
        entry = types.get(rel_type, {})
        return float(entry.get("default_weight", 0.5))

    def get_inverse(self, rel_type: str) -> str | None:
        """Return the inverse relation type, or None."""
        types = self._data.get("relation_types", {})
        entry = types.get(rel_type)
        if entry:
            return entry.get("inverse")
        return None

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def validate_relationship_type(self, rel_type: str) -> str:
        """Return the relationship type if valid, otherwise fall back to 'related_to'."""
        if self.is_valid_relationship_type(rel_type):
            return rel_type
        return "related_to"

    # ------------------------------------------------------------------
    # Evolution config
    # ------------------------------------------------------------------

    def get_evolution_config(self) -> dict[str, Any]:
        """Return the evolution configuration."""
        return dict(self._data.get("evolution_config", {}))

    # ------------------------------------------------------------------
    # Changelog
    # ------------------------------------------------------------------

    async def _append_changelog(self, operation: str, detail: str) -> None:
        """Append an entry to ontology-changelog.md next to ontology.yaml."""
        changelog_path = self._path.parent / "ontology-changelog.md"
        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        version = self.schema_version
        entry = f"\n## {today} — schema v{version}\n\n### {operation}\n- {detail}\n"

        def _write() -> None:
            if changelog_path.exists():
                with open(changelog_path, "a", encoding="utf-8") as f:
                    f.write(entry)
            else:
                changelog_path.write_text(f"# Ontology Changelog\n{entry}", encoding="utf-8")

        await asyncio.to_thread(_write)

    # ------------------------------------------------------------------
    # Schema metadata
    # ------------------------------------------------------------------

    @property
    def schema_version(self) -> int:
        """The ontology schema version."""
        return int(self._data.get("schema_version", 1))

    # Keep old property for transition
    @property
    def version(self) -> str:
        return str(self.schema_version)
