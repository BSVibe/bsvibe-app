"""TagResolver — Handoff §11 raw-tag → concept-id resolution.

The resolver does NOT mutate the vault. Auto-applying a ``CreateConcept``
for a ``new_candidate`` outcome is the service's job; the resolver only
classifies. This separation lets ingest/REST/MCP consumers all share the
same classification surface.
"""

from __future__ import annotations

import re

from backend.knowledge.canonicalization import models, paths
from backend.knowledge.canonicalization.index import CanonicalizationIndex

_NORMALIZE_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_REDIRECT_DEPTH_LIMIT = 16


class TagResolver:
    """Classify a raw tag against the canonicalization index (Handoff §11)."""

    def __init__(self, index: CanonicalizationIndex) -> None:
        self._index = index

    @staticmethod
    def normalize(raw_tag: str) -> str:
        """Deterministic normalization: trim, lowercase, separator collapse.

        Returns "" if the input contains no usable id characters.
        """
        if not isinstance(raw_tag, str):
            return ""
        # casefold → replace any non-[a-z0-9] run with single hyphen → strip
        return _NORMALIZE_NON_ALNUM.sub("-", raw_tag.casefold()).strip("-")

    async def resolve(self, raw_tag: str) -> models.ResolveResult:
        normalized = self.normalize(raw_tag)
        if not normalized:
            return models.ResolveResult(status="blocked")
        if not paths.is_valid_concept_id(normalized):
            return models.ResolveResult(status="blocked")

        # 1. Exact active concept id match
        active = await self._index.get_active_concept(normalized)
        if active is not None:
            return models.ResolveResult(status="resolved", concept_id=active.concept_id)

        # 2. Active alias match
        alias_hits = await self._index.find_concepts_by_alias(normalized)
        if len(alias_hits) == 1:
            return models.ResolveResult(status="resolved", concept_id=alias_hits[0].concept_id)
        if len(alias_hits) > 1:
            return models.ResolveResult(
                status="ambiguous",
                ambiguous_candidates=sorted(c.concept_id for c in alias_hits),
            )

        # 3. Tombstone redirect to active merged_into
        ts = await self._index.get_tombstone(normalized)
        if ts is not None:
            target = await self._follow_redirect_chain(ts.merged_into, {normalized})
            if target is not None:
                return models.ResolveResult(
                    status="resolved",
                    concept_id=target,
                    redirected_from=normalized,
                )
            return models.ResolveResult(status="blocked", redirected_from=normalized)

        # 4. Deprecated → blocked + replacement suggestion
        dep = await self._index.get_deprecated(normalized)
        if dep is not None:
            return models.ResolveResult(
                status="blocked",
                deprecated_replacement=dep.replacement,
            )

        # 5. Pending non-terminal CreateConcept draft
        pending = await self._index.find_pending_concept_draft(normalized)
        if pending is not None:
            return models.ResolveResult(
                status="pending_candidate",
                concept_id=normalized,
                pending_draft=pending.path,
            )

        # 6. No match → new candidate
        return models.ResolveResult(status="new_candidate", concept_id=normalized)

    async def _follow_redirect_chain(self, target_id: str, visited: set[str]) -> str | None:
        """Walk tombstone redirect chain until landing on an active concept.

        Returns the active concept id, or None if the chain hits a cycle,
        a missing target, or a deprecated/non-active terminus.
        """
        current = target_id
        depth = 0
        while True:
            if depth >= _REDIRECT_DEPTH_LIMIT:
                return None
            if current in visited:
                return None
            visited.add(current)

            active = await self._index.get_active_concept(current)
            if active is not None:
                return active.concept_id

            ts = await self._index.get_tombstone(current)
            if ts is not None:
                current = ts.merged_into
                depth += 1
                continue

            # Missing or deprecated terminus — invalid redirect target
            return None
