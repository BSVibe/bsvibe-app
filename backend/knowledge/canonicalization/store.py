"""NoteStore — typed wrapper over StorageBackend (Class_Diagram §5).

Reuses ``markdown_utils.extract_frontmatter`` / ``extract_title`` /
``body_after_frontmatter`` and ``note.build_frontmatter`` (per Class_Diagram §10).
Handles ISO 8601 datetime serialization (Handoff §2) and frontmatter shape
discipline (Handoff §0.2 — path/frontmatter have different jobs).

Lift M2 (v8 §20.3 Pattern B audit, 2026-06-02) — **NOT a Pattern B
violation, skipped.** This is a typed-storage Repository facade: pure
read/write per record kind (concept / action / proposal / decision /
policy / garden / tombstone). No state-advance, no validation beyond
frontmatter shape parsing. Module-level helpers (``_iso``, ``_parse_iso``,
``_drop_nones``, ``_serialize_record``) already extracted. Path-derived
kind classifiers (``_*_kind_from_path``) are tightly coupled to read
parsers and stay with the Repository.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from backend.knowledge.canonicalization import models, paths
from backend.knowledge.graph.markdown_utils import (
    body_after_frontmatter,
    extract_frontmatter,
    extract_title,
)
from backend.knowledge.graph.note import build_frontmatter
from backend.knowledge.graph.storage import StorageBackend


def _iso(dt: datetime) -> str:
    """ISO 8601 with timezone where present (Handoff §2)."""
    return dt.isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    msg = f"unsupported datetime value: {value!r}"
    raise TypeError(msg)


def _drop_nones(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively drop None entries so YAML stays clean."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, dict):
            out[k] = _drop_nones(v)
        else:
            out[k] = v
    return out


def _serialize_record(record: Any) -> dict[str, Any]:
    """Serialize a nested dataclass record, converting datetime → ISO."""
    raw = asdict(record)
    return {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in raw.items()}


class NoteStore:
    """Typed read/write helpers for canonicalization notes.

    Slice 1 implements concepts (active) + actions + garden tag mutation only.
    Proposals/decisions/policies/tombstones come in later slices.
    """

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    # ------------------------------------------------------------------ concepts

    async def concept_exists(self, concept_id: str) -> bool:
        return await self._storage.exists(paths.active_concept_path(concept_id))

    async def read_concept(self, concept_id: str) -> models.ConceptEntry | None:
        path = paths.active_concept_path(concept_id)
        if not await self._storage.exists(path):
            return None
        text = await self._storage.read(path)
        fm = extract_frontmatter(text)
        return models.ConceptEntry(
            concept_id=concept_id,
            path=path,
            display=extract_title(text),
            aliases=list(fm.get("aliases") or []),
            created_at=_parse_iso(fm.get("created_at")) or datetime.min,
            updated_at=_parse_iso(fm.get("updated_at")) or datetime.min,
            source_action=fm.get("source_action"),
            # Lift E26 — read the seedling note kind back if present.
            note_type=fm.get("type"),
        )

    async def write_concept(
        self,
        entry: models.ConceptEntry,
        initial_body: str | None = None,
    ) -> None:
        # Handoff §3.1: aliases / created_at / updated_at / source_action only.
        # Forbidden: status, concept_id, canonical_tag, display, bsage_role, graph_scope.
        fm: dict[str, Any] = {
            "created_at": _iso(entry.created_at),
            "updated_at": _iso(entry.updated_at),
        }
        if entry.aliases:
            fm["aliases"] = list(entry.aliases)
        if entry.source_action is not None:
            fm["source_action"] = entry.source_action
        # Lift E26 — carry the seedling note kind through to the concept
        # so the founder can tell a Pattern from a Principle/DomainModel/
        # TechInsight at a glance. Pre-E26 promotion dropped this.
        if entry.note_type is not None:
            fm["type"] = entry.note_type

        body_lines = [f"# {entry.display}", ""]
        if initial_body:
            body_lines.append(initial_body.rstrip() + "\n")
        body = "\n".join(body_lines)
        text = build_frontmatter(fm) + body
        await self._storage.write(entry.path, text)

    # ------------------------------------------------------------------- actions

    async def read_action(self, path: str) -> models.ActionEntry | None:
        if not await self._storage.exists(path):
            return None
        text = await self._storage.read(path)
        fm = extract_frontmatter(text)
        kind = self._action_kind_from_path(path)

        validation_fm = fm.get("validation") or {}
        scoring_fm = fm.get("scoring") or {}
        permission_fm = fm.get("permission") or {}
        execution_fm = fm.get("execution") or {}

        return models.ActionEntry(
            path=path,
            kind=kind,
            status=fm.get("status", "draft"),
            action_schema_version=fm.get("action_schema_version", ""),
            params=dict(fm.get("params") or {}),
            created_at=_parse_iso(fm.get("created_at")) or datetime.min,
            updated_at=_parse_iso(fm.get("updated_at")) or datetime.min,
            expires_at=_parse_iso(fm.get("expires_at")) or datetime.min,
            source_proposal=fm.get("source_proposal"),
            freshness=dict(fm.get("freshness") or {}),
            validation=models.ValidationResult(
                status=validation_fm.get("status", "not_run"),
                hard_blocks=list(validation_fm.get("hard_blocks") or []),
            ),
            scoring=models.ScoreResult(
                status=scoring_fm.get("status", "not_run"),
                stability_score=scoring_fm.get("stability_score"),
                scorer_version=scoring_fm.get("scorer_version"),
                policy_profile_path=scoring_fm.get("policy_profile_path"),
                hard_blocks=list(scoring_fm.get("hard_blocks") or []),
                risk_reasons=list(scoring_fm.get("risk_reasons") or []),
                deterministic_evidence=list(scoring_fm.get("deterministic_evidence") or []),
                model_evidence=list(scoring_fm.get("model_evidence") or []),
                human_evidence=list(scoring_fm.get("human_evidence") or []),
            ),
            permission=models.PermissionRecord(
                safe_mode=permission_fm.get("safe_mode"),
                decision=permission_fm.get("decision"),
                actor=permission_fm.get("actor"),
                decided_at=_parse_iso(permission_fm.get("decided_at")),
            ),
            execution=models.ExecutionRecord(
                status=execution_fm.get("status", "not_run"),
                applied_at=_parse_iso(execution_fm.get("applied_at")),
                error=execution_fm.get("error"),
            ),
            affected_paths=list(fm.get("affected_paths") or []),
            supersedes=list(fm.get("supersedes") or []),
            superseded_by=fm.get("superseded_by"),
            evidence=list(fm.get("evidence") or []),
        )

    async def write_action(self, entry: models.ActionEntry, body: str = "") -> None:
        # Handoff §0.2: kind is path-derived. Do NOT write action_type.
        fm: dict[str, Any] = {
            "status": entry.status,
            "created_at": _iso(entry.created_at),
            "updated_at": _iso(entry.updated_at),
            "expires_at": _iso(entry.expires_at),
            "action_schema_version": entry.action_schema_version,
            "params": entry.params,
            "freshness": entry.freshness,
            "validation": _serialize_record(entry.validation),
            "scoring": _serialize_record(entry.scoring),
            "permission": _drop_nones(_serialize_record(entry.permission)),
            "execution": _drop_nones(_serialize_record(entry.execution)),
            "affected_paths": list(entry.affected_paths),
            "supersedes": list(entry.supersedes),
            "superseded_by": entry.superseded_by,
            "evidence": list(entry.evidence),
        }
        if entry.source_proposal is not None:
            fm["source_proposal"] = entry.source_proposal

        text = build_frontmatter(fm) + (body or "")
        await self._storage.write(entry.path, text)

    async def list_existing_action_paths(self, action_kind: str) -> set[str]:
        return set(await self._storage.list_files(f"actions/{action_kind}", "*.md"))

    # ----------------------------------------------------------------- proposals

    async def read_proposal(self, path: str) -> models.ProposalEntry | None:
        if not await self._storage.exists(path):
            return None
        text = await self._storage.read(path)
        fm = extract_frontmatter(text)
        kind = self._proposal_kind_from_path(path)
        return models.ProposalEntry(
            path=path,
            kind=kind,
            status=fm.get("status", "pending"),
            strategy=fm.get("strategy", ""),
            generator=fm.get("generator", ""),
            generator_version=fm.get("generator_version", ""),
            proposal_score=float(fm.get("proposal_score") or 0.0),
            created_at=_parse_iso(fm.get("created_at")) or datetime.min,
            updated_at=_parse_iso(fm.get("updated_at")) or datetime.min,
            expires_at=_parse_iso(fm.get("expires_at")) or datetime.min,
            freshness=dict(fm.get("freshness") or {}),
            evidence=list(fm.get("evidence") or []),
            affected_paths=list(fm.get("affected_paths") or []),
            action_drafts=list(fm.get("action_drafts") or []),
            result_actions=list(fm.get("result_actions") or []),
        )

    async def write_proposal(self, entry: models.ProposalEntry, body: str = "") -> None:
        # Handoff §0.2: proposal kind is path-derived. Do NOT write proposal_type.
        # Handoff §5: proposals MUST NOT contain executable params.
        fm: dict[str, Any] = {
            "status": entry.status,
            "created_at": _iso(entry.created_at),
            "updated_at": _iso(entry.updated_at),
            "expires_at": _iso(entry.expires_at),
            "strategy": entry.strategy,
            "generator": entry.generator,
            "generator_version": entry.generator_version,
            "proposal_score": entry.proposal_score,
            "freshness": entry.freshness,
            "evidence": list(entry.evidence),
            "affected_paths": list(entry.affected_paths),
            "action_drafts": list(entry.action_drafts),
            "result_actions": list(entry.result_actions),
        }
        text = build_frontmatter(fm) + (body or "")
        await self._storage.write(entry.path, text)

    async def list_existing_proposal_paths(self, proposal_kind: str) -> set[str]:
        return set(await self._storage.list_files(f"proposals/{proposal_kind}", "*.md"))

    # ---------------------------------------------------------------- tombstones

    async def write_tombstone(
        self,
        old_id: str,
        merged_into: str,
        merged_at: datetime,
        source_action: str | None = None,
        display: str | None = None,
    ) -> str:
        """Create ``concepts/merged/<old-id>.md`` (Handoff §3.2)."""
        path = f"concepts/merged/{old_id}.md"
        fm: dict[str, Any] = {
            "merged_into": merged_into,
            "merged_at": _iso(merged_at),
        }
        if source_action is not None:
            fm["source_action"] = source_action
        body = f"# {display or old_id}\n"
        text = build_frontmatter(fm) + body
        await self._storage.write(path, text)
        return path

    async def delete_active_concept(self, concept_id: str) -> None:
        await self._storage.delete(f"concepts/active/{concept_id}.md")

    async def list_garden_paths(self) -> list[str]:
        """All garden notes across maturity folders."""
        return list(await self._storage.list_files("garden", "*.md"))

    # ----------------------------------------------------------------- decisions

    async def read_decision(self, path: str) -> models.DecisionEntry | None:
        if not await self._storage.exists(path):
            return None
        text = await self._storage.read(path)
        fm = extract_frontmatter(text)
        kind = self._decision_kind_from_path(path)
        decay_fm = fm.get("decay") or {}
        return models.DecisionEntry(
            path=path,
            kind=kind,
            status=fm.get("status", "active"),
            maturity=fm.get("maturity", "seedling"),
            decision_schema_version=fm.get("decision_schema_version", ""),
            subjects=tuple(fm.get("subjects") or ()),
            base_confidence=float(fm.get("base_confidence") or 0.0),
            last_confirmed_at=_parse_iso(fm.get("last_confirmed_at")) or datetime.min,
            decay_profile=decay_fm.get("profile", "definitional"),
            decay_halflife_days=decay_fm.get("halflife_days"),
            valid_from=_parse_iso(fm.get("valid_from")) or datetime.min,
            created_at=_parse_iso(fm.get("created_at")) or datetime.min,
            updated_at=_parse_iso(fm.get("updated_at")) or datetime.min,
            review_after=_parse_iso(fm.get("review_after")),
            expires_at=_parse_iso(fm.get("expires_at")),
            policy_profile_path=fm.get("policy_profile_path"),
            source_proposal=fm.get("source_proposal"),
            source_action=fm.get("source_action"),
            supersedes=list(fm.get("supersedes") or []),
            superseded_by=fm.get("superseded_by"),
        )

    async def write_decision(self, entry: models.DecisionEntry, body: str = "") -> None:
        # Handoff §0.2: decision kind is path-derived. No decision_type.
        fm: dict[str, Any] = {
            "status": entry.status,
            "maturity": entry.maturity,
            "created_at": _iso(entry.created_at),
            "updated_at": _iso(entry.updated_at),
            "decision_schema_version": entry.decision_schema_version,
            "subjects": list(entry.subjects),
            "base_confidence": entry.base_confidence,
            "last_confirmed_at": _iso(entry.last_confirmed_at),
            "decay": {
                "profile": entry.decay_profile,
                "halflife_days": entry.decay_halflife_days,
            },
            "valid_from": _iso(entry.valid_from),
            "review_after": _iso(entry.review_after) if entry.review_after else None,
            "expires_at": _iso(entry.expires_at) if entry.expires_at else None,
            "supersedes": list(entry.supersedes),
            "superseded_by": entry.superseded_by,
        }
        if entry.policy_profile_path is not None:
            fm["policy_profile_path"] = entry.policy_profile_path
        if entry.source_proposal is not None:
            fm["source_proposal"] = entry.source_proposal
        if entry.source_action is not None:
            fm["source_action"] = entry.source_action
        text = build_frontmatter(fm) + (body or "")
        await self._storage.write(entry.path, text)

    async def list_existing_decision_paths(self, decision_kind: str) -> set[str]:
        return set(await self._storage.list_files(f"decisions/{decision_kind}", "*.md"))

    # ------------------------------------------------------------------ policies

    async def read_policy(self, path: str) -> models.PolicyEntry | None:
        if not await self._storage.exists(path):
            return None
        text = await self._storage.read(path)
        fm = extract_frontmatter(text)
        kind = self._policy_kind_from_path(path)
        return models.PolicyEntry(
            path=path,
            kind=kind,
            status=fm.get("status", "active"),
            profile_name=fm.get("profile_name", ""),
            priority=int(fm.get("priority") or 0),
            scope=dict(fm.get("scope") or {}),
            policy_schema_version=fm.get("policy_schema_version", ""),
            valid_from=_parse_iso(fm.get("valid_from")) or datetime.min,
            params=dict(fm.get("params") or {}),
            created_at=_parse_iso(fm.get("created_at")) or datetime.min,
            updated_at=_parse_iso(fm.get("updated_at")) or datetime.min,
            expires_at=_parse_iso(fm.get("expires_at")),
            learned_from=dict(fm.get("learned_from") or {}),
            supersedes=list(fm.get("supersedes") or []),
            superseded_by=fm.get("superseded_by"),
        )

    async def write_policy(self, entry: models.PolicyEntry, body: str = "") -> None:
        # Handoff §0.2: policy kind is path-derived. No policy_type.
        fm: dict[str, Any] = {
            "status": entry.status,
            "created_at": _iso(entry.created_at),
            "updated_at": _iso(entry.updated_at),
            "policy_schema_version": entry.policy_schema_version,
            "profile_name": entry.profile_name,
            "priority": entry.priority,
            "scope": dict(entry.scope),
            "valid_from": _iso(entry.valid_from),
            "expires_at": _iso(entry.expires_at) if entry.expires_at else None,
            "params": dict(entry.params),
            "learned_from": dict(entry.learned_from),
            "supersedes": list(entry.supersedes),
            "superseded_by": entry.superseded_by,
        }
        text = build_frontmatter(fm) + (body or "")
        await self._storage.write(entry.path, text)

    async def list_existing_policy_paths(self, policy_kind: str) -> set[str]:
        return set(await self._storage.list_files(f"decisions/policy/{policy_kind}", "*.md"))

    # ------------------------------------------------------------------- garden

    async def read_garden_tags(self, garden_path: str) -> list[str]:
        if not await self._storage.exists(garden_path):
            msg = f"garden note not found: {garden_path}"
            raise FileNotFoundError(msg)
        text = await self._storage.read(garden_path)
        fm = extract_frontmatter(text)
        return list(fm.get("tags") or [])

    async def read_garden_note_type(self, garden_path: str) -> str | None:
        """Lift E26 — return the seedling's ``type:`` frontmatter field.

        E20 stamps one of ``Pattern`` / ``Principle`` / ``TechInsight`` /
        ``DomainModel`` on every seedling. The promoter uses this to
        determine the dominant kind across the seedlings that contributed
        to a candidate tag, then carries it through to the concept the
        promotion creates. Returns ``None`` for legacy / pre-E20 notes
        and on a missing file (treated like an absent type rather than
        raising, since the promoter walks many notes best-effort).
        """
        try:
            text = await self._storage.read(garden_path)
        except FileNotFoundError:
            return None
        fm = extract_frontmatter(text)
        value = fm.get("type")
        return value if isinstance(value, str) and value else None

    async def set_garden_tags(self, garden_path: str, tags: list[str]) -> None:
        """Replace ``tags`` frontmatter on a garden note (Handoff §7.6)."""
        if await self._storage.exists(garden_path):
            text = await self._storage.read(garden_path)
        else:
            text = ""
        fm = extract_frontmatter(text)
        body = body_after_frontmatter(text)
        fm["tags"] = list(tags)
        # If body is empty (no frontmatter present originally), ensure clean output
        new_text = build_frontmatter(fm) + body
        await self._storage.write(garden_path, new_text)

    # ------------------------------------------------------------------- helpers

    @staticmethod
    def _action_kind_from_path(path: str) -> str:
        parts = PurePosixPath(path).parts
        if len(parts) < 3 or parts[0] != "actions":
            msg = f"not an action path: {path!r}"
            raise ValueError(msg)
        return parts[1]

    @staticmethod
    def _proposal_kind_from_path(path: str) -> str:
        parts = PurePosixPath(path).parts
        if len(parts) < 3 or parts[0] != "proposals":
            msg = f"not a proposal path: {path!r}"
            raise ValueError(msg)
        return parts[1]

    @staticmethod
    def _decision_kind_from_path(path: str) -> str:
        parts = PurePosixPath(path).parts
        if len(parts) < 3 or parts[0] != "decisions" or parts[1] == "policy":
            msg = f"not a decision path: {path!r}"
            raise ValueError(msg)
        return parts[1]

    @staticmethod
    def _policy_kind_from_path(path: str) -> str:
        # decisions/policy/<policy-kind>/<profile>.md
        parts = PurePosixPath(path).parts
        if len(parts) < 4 or parts[0] != "decisions" or parts[1] != "policy":
            msg = f"not a policy path: {path!r}"
            raise ValueError(msg)
        return parts[2]
