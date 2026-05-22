"""canon-lint — orphan tag / alias collision / redirect anomaly detection.

Slice 6 module that mirrors ``bsage.garden.vault_linter.VaultLinter``
pattern (Class_Diagram §10.2 reuse map): scan + collect findings.

Per Handoff §15.3 (canon-lint plugin):
- Orphan garden tags MUST surface as lint findings
- Alias collisions across active concepts MUST surface
- Redirect chain anomalies (cycles, missing targets) MUST surface

Findings here are read-only — they do NOT mutate the vault. The
``canon-lint`` plugin shim renders them as a markdown report; future
slices may layer auto-create-proposal-from-finding behavior on top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.knowledge.canonicalization.index import CanonicalizationIndex
from backend.knowledge.canonicalization.store import NoteStore

_REDIRECT_DEPTH_LIMIT = 16


@dataclass
class LintFinding:
    """Single lint observation. Source-aware ``severity`` mirrors the
    Handoff §13 source separation idea: ``error`` for spec-invariant
    violations, ``warning`` for review-worthy heuristics."""

    kind: str
    severity: str = "warning"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class LintReport:
    findings: list[LintFinding] = field(default_factory=list)

    @property
    def orphan_tag_count(self) -> int:
        return sum(1 for f in self.findings if f.kind == "orphan_tag")

    @property
    def alias_collision_count(self) -> int:
        return sum(1 for f in self.findings if f.kind == "alias_collision")

    @property
    def redirect_anomaly_count(self) -> int:
        return sum(
            1 for f in self.findings if f.kind in {"redirect_cycle", "redirect_target_missing"}
        )


# ---------------------------------------------------------------- detectors


async def find_orphan_tags(index: CanonicalizationIndex, store: NoteStore) -> list[LintFinding]:
    """Garden tags that don't resolve to an active concept (or its
    aliases / a redirect-resolved tombstone target)."""
    # Pre-build the set of "known" identifiers: every active concept_id
    # and every alias case-folded.
    active = await index.list_active_concepts()
    known: set[str] = set()
    for c in active:
        known.add(c.concept_id.casefold())
        for a in c.aliases:
            known.add(a.casefold())

    # Group orphan tag → list of garden paths using it
    orphans: dict[str, list[str]] = {}
    for garden_path in await store.list_garden_paths():
        tags = await store.read_garden_tags(garden_path)
        for tag in tags:
            if not isinstance(tag, str):
                continue
            tag_norm = tag.casefold()
            if tag_norm in known:
                continue
            # Tombstone redirect resolves? Walk the chain.
            if await _redirects_to_active(index, tag, depth_left=_REDIRECT_DEPTH_LIMIT):
                continue
            orphans.setdefault(tag, []).append(garden_path)

    return [
        LintFinding(
            kind="orphan_tag",
            severity="warning",
            payload={"tag": tag, "garden_paths": sorted(paths)},
        )
        for tag, paths in sorted(orphans.items())
    ]


async def find_alias_collisions(
    index: CanonicalizationIndex,
) -> list[LintFinding]:
    """Aliases shared by ≥2 active concepts. Surfaces the same condition
    that the resolver returns as ``ambiguous`` — but visible at lint
    time so operators can review proactively."""
    by_alias: dict[str, list[str]] = {}
    for c in await index.list_active_concepts():
        for alias in c.aliases:
            by_alias.setdefault(alias.casefold(), []).append(c.concept_id)
    findings: list[LintFinding] = []
    for alias, concept_ids in sorted(by_alias.items()):
        if len(set(concept_ids)) < 2:
            continue
        findings.append(
            LintFinding(
                kind="alias_collision",
                severity="warning",
                payload={"alias": alias, "concepts": sorted(set(concept_ids))},
            )
        )
    return findings


async def find_redirect_anomalies(
    index: CanonicalizationIndex,
) -> list[LintFinding]:
    """Tombstone redirect chains that cycle or land on non-active
    targets. Per Handoff §3.2 — ``merged_into`` MUST point at active.
    """
    findings: list[LintFinding] = []
    # The InMemory impl exposes ``_tombstones`` as the canonical source;
    # we consult it directly because ``CanonicalizationIndex`` doesn't
    # have a public ``list_tombstones`` (intentional: redirects are
    # consulted via ``get_tombstone(old_id)`` only). Iterating private
    # state is acceptable here since this is the sister module.
    tombstones: dict[str, Any] = getattr(index, "_tombstones", {}) or {}
    for old_id, ts in tombstones.items():
        target = ts.merged_into
        visited: set[str] = {old_id}
        depth = 0
        while True:
            if depth >= _REDIRECT_DEPTH_LIMIT:
                findings.append(
                    LintFinding(
                        kind="redirect_cycle",
                        severity="error",
                        payload={"old_id": old_id, "depth_limit": depth},
                    )
                )
                break
            if target in visited:
                findings.append(
                    LintFinding(
                        kind="redirect_cycle",
                        severity="error",
                        payload={
                            "old_id": old_id,
                            "cycle_through": sorted(visited | {target}),
                        },
                    )
                )
                break
            visited.add(target)
            active = await index.get_active_concept(target)
            if active is not None:
                break  # clean — terminates at active
            next_ts = await index.get_tombstone(target)
            if next_ts is None:
                findings.append(
                    LintFinding(
                        kind="redirect_target_missing",
                        severity="error",
                        payload={"old_id": old_id, "missing_target": target},
                    )
                )
                break
            target = next_ts.merged_into
            depth += 1
    return findings


# ---------------------------------------------------------------- aggregator


async def run_lint(index: CanonicalizationIndex, store: NoteStore) -> LintReport:
    """Run all canon lint detectors and aggregate."""
    findings: list[LintFinding] = []
    findings.extend(await find_orphan_tags(index, store))
    findings.extend(await find_alias_collisions(index))
    findings.extend(await find_redirect_anomalies(index))
    return LintReport(findings=findings)


# ---------------------------------------------------------------- helpers


async def _redirects_to_active(index: CanonicalizationIndex, tag: str, *, depth_left: int) -> bool:
    """True iff ``tag`` walks through tombstones to an active concept."""
    visited: set[str] = set()
    current = tag
    while depth_left > 0:
        if current in visited:
            return False
        visited.add(current)
        active = await index.get_active_concept(current)
        if active is not None:
            return True
        ts = await index.get_tombstone(current)
        if ts is None:
            return False
        current = ts.merged_into
        depth_left -= 1
    return False
