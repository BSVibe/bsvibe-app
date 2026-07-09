"""Action effects — Handoff §13 step 12 (persistent mutation).

Mixin extracted from the original ``service.py`` god-file per v8 §17.4.
Holds the per-kind persistence logic (concept create, retag, merge, decision
create). Each effect MUST be idempotent — re-running an applied action over
the same vault state produces the same outcome.

Mixin contract:
- Depends on ``self._store`` (write_concept, write_decision, etc.)
- Depends on ``self._clock`` (effect timestamps)

Note: effects run AFTER validation + scoring + Safe Mode permission check
inside ``_apply_locked``. Effects MUST NOT re-validate — the contract is
that validation already passed when ``_persist_effects`` is invoked.
"""

from __future__ import annotations

from backend.knowledge.canonicalization import models, paths
from backend.knowledge.canonicalization.service._base import _ServiceBase

# Default policies for MergeConcepts when params omit them (Handoff §7.2).
_DEFAULT_MERGE_ALIAS_POLICY = {
    "add_merged_ids_as_aliases": True,
    "preserve_existing_aliases": True,
}
_DEFAULT_MERGE_TOMBSTONE_POLICY = {"create_merged_notes": True}
_DEFAULT_MERGE_RETAG_POLICY = {"update_garden_tags": True}


class _EffectsMixin(_ServiceBase):
    """Per-kind persistent effects for applied actions.

    The single public-to-the-mixin entry is ``_persist_effects``; it
    dispatches to one of the per-kind effect handlers. Each returns the
    list of paths that were touched, used by the caller to invalidate
    the canonicalization index.
    """

    async def _persist_effects(self, entry: models.ActionEntry) -> list[str]:
        if entry.kind == "create-concept":
            return await self._effect_create_concept(entry)
        if entry.kind == "retag-notes":
            return await self._effect_retag_notes(entry)
        if entry.kind == "merge-concepts":
            return await self._effect_merge_concepts(entry)
        if entry.kind == "create-decision":
            return await self._effect_create_decision(entry)
        msg = f"unsupported kind: {entry.kind!r}"  # pragma: no cover
        raise NotImplementedError(msg)

    async def _effect_create_concept(self, entry: models.ActionEntry) -> list[str]:
        concept = entry.params["concept"]
        title = entry.params["title"]
        aliases = list(entry.params.get("aliases") or [])
        initial_body = entry.params.get("initial_body")
        # Lift E26 — promotion stamps the dominant seedling type onto the
        # action's params so the concept inherits the same E20 ``type:``
        # field. Empty / unset = legacy concept, no type written.
        note_type = entry.params.get("type") or None
        # Per-locale display labels (founder decision 2026-07) — promotion /
        # backfill stamp ``{lang: label}`` so the graph node renders in the
        # workspace language while the id + H1 stay the English identifier.
        display_labels = {
            str(k): str(v)
            for k, v in (entry.params.get("display_labels") or {}).items()
            if isinstance(v, str) and v.strip()
        }

        now = self._clock()
        path = paths.active_concept_path(concept)
        await self._store.write_concept(
            models.ConceptEntry(
                concept_id=concept,
                path=path,
                display=title,
                aliases=aliases,
                created_at=now,
                updated_at=now,
                source_action=entry.path,
                note_type=note_type,
                display_labels=display_labels,
            ),
            initial_body=initial_body,
        )
        return [path]

    async def _effect_retag_notes(self, entry: models.ActionEntry) -> list[str]:
        affected: list[str] = []
        for change in entry.params["changes"]:
            path = change["path"]
            current = await self._store.read_garden_tags(path)
            remove = set(change.get("remove_tags") or [])
            add = list(change.get("add_tags") or [])
            kept = [t for t in current if t not in remove]
            merged: list[str] = []
            seen: set[str] = set()
            for tag in [*kept, *add]:
                if tag not in seen:
                    seen.add(tag)
                    merged.append(tag)
            merged.sort()
            await self._store.set_garden_tags(path, merged)
            affected.append(path)
        return affected

    async def _effect_merge_concepts(self, entry: models.ActionEntry) -> list[str]:
        params = entry.params
        canonical_id: str = params["canonical"]
        merge_ids: list[str] = list(params["merge"])
        alias_policy = {**_DEFAULT_MERGE_ALIAS_POLICY, **(params.get("alias_policy") or {})}
        tombstone_policy = {
            **_DEFAULT_MERGE_TOMBSTONE_POLICY,
            **(params.get("tombstone_policy") or {}),
        }
        retag_policy = {
            **_DEFAULT_MERGE_RETAG_POLICY,
            **(params.get("retag_policy") or {}),
        }

        affected: list[str] = []
        now = self._clock()

        canonical = await self._store.read_concept(canonical_id)
        if canonical is None:
            msg = f"canonical concept disappeared mid-apply: {canonical_id!r}"
            raise RuntimeError(msg)

        # 1. Read all merge sources before touching anything
        sources: dict[str, models.ConceptEntry] = {}
        for old_id in merge_ids:
            entry_old = await self._store.read_concept(old_id)
            if entry_old is None:
                msg = f"merge source disappeared mid-apply: {old_id!r}"
                raise RuntimeError(msg)
            sources[old_id] = entry_old

        # 2. Update canonical aliases
        new_aliases: list[str] = (
            list(canonical.aliases) if alias_policy.get("preserve_existing_aliases", True) else []
        )
        if alias_policy.get("add_merged_ids_as_aliases", True):
            for old_id, src in sources.items():
                if old_id not in new_aliases:
                    new_aliases.append(old_id)
                for alias in src.aliases:
                    if alias not in new_aliases:
                        new_aliases.append(alias)
        canonical.aliases = new_aliases
        canonical.updated_at = now
        await self._store.write_concept(canonical)
        affected.append(canonical.path)

        # 3. Tombstones + delete old active notes
        for old_id, src in sources.items():
            old_active_path = f"concepts/active/{old_id}.md"
            await self._store.delete_active_concept(old_id)
            affected.append(old_active_path)
            if tombstone_policy.get("create_merged_notes", True):
                tombstone_path = await self._store.write_tombstone(
                    old_id=old_id,
                    merged_into=canonical_id,
                    merged_at=now,
                    source_action=entry.path,
                    display=src.display or old_id,
                )
                affected.append(tombstone_path)

        # 4. Garden retag
        if retag_policy.get("update_garden_tags", True):
            merge_set = set(merge_ids)
            for garden_path in await self._store.list_garden_paths():
                tags = await self._store.read_garden_tags(garden_path)
                if not any(t in merge_set for t in tags):
                    continue
                rewritten: list[str] = []
                seen: set[str] = set()
                for tag in tags:
                    new = canonical_id if tag in merge_set else tag
                    if new not in seen:
                        seen.add(new)
                        rewritten.append(new)
                await self._store.set_garden_tags(garden_path, rewritten)
                affected.append(garden_path)

        return affected

    async def _effect_create_decision(self, entry: models.ActionEntry) -> list[str]:
        params = entry.params
        decision_path: str = params["decision_path"]
        decision_kind = decision_path.split("/")[1]
        subjects = tuple(s for s in params["subjects"] if isinstance(s, str))
        base_confidence = float(params["base_confidence"])
        maturity = params["maturity"]
        decay_profile = params.get("decay_profile") or (
            "definitional" if decision_kind == "cannot-link" else "semantic"
        )
        decay_halflife_days = params.get("decay_halflife_days")
        supersedes = list(params.get("supersedes") or [])

        now = self._clock()
        affected: list[str] = []

        # 1. Write the new decision note
        decision = models.DecisionEntry(
            path=decision_path,
            kind=decision_kind,
            status="active",
            maturity=maturity,
            decision_schema_version=f"{decision_kind}-v1",
            subjects=subjects,
            base_confidence=base_confidence,
            last_confirmed_at=now,
            decay_profile=decay_profile,
            decay_halflife_days=decay_halflife_days,
            valid_from=now,
            created_at=now,
            updated_at=now,
            policy_profile_path=params.get("policy_profile_path"),
            source_proposal=params.get("source_proposal"),
            source_action=entry.path,
            supersedes=supersedes,
        )
        await self._store.write_decision(decision)
        affected.append(decision_path)

        # 2. Mark each superseded decision (Handoff §7.8 — both paths in affected)
        for sup_path in supersedes:
            existing = await self._store.read_decision(sup_path)
            if existing is None:
                continue
            existing.status = "superseded"
            existing.superseded_by = decision_path
            existing.updated_at = now
            await self._store.write_decision(existing)
            affected.append(sup_path)

        return affected
