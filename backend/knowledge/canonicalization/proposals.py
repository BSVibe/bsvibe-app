"""DeterministicProposer — slice 3 proposal generation (Handoff §12).

Strategy ``deterministic`` uses preprocessing-only signals:
- exact alias collision between active concepts
- character-trigram Jaccard similarity on concept ids (simple lexical proxy)
- garden-tag frequency for canonical selection

No embeddings, no LLM, no Levenshtein (per spec §12 — Levenshtein is not a
default signal). The ``balanced`` strategy in slice 4 layers embedding KNN
+ EDC LLM verify on top of these same outputs.

Per §12, proposal generation MAY create draft action notes through
``service.create_action_draft``. We do exactly that — each proposal links
to a paired ``actions/merge-concepts/...md`` draft via ``action_drafts``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

import structlog

from backend.knowledge.canonicalization import models, paths
from backend.knowledge.canonicalization.evidence import envelope as _evidence_envelope
from backend.knowledge.canonicalization.index import CanonicalizationIndex
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.retrieval.vector_store import _cosine_similarity

logger = structlog.get_logger(__name__)

_NGRAM_N = 3
_DEFAULT_THRESHOLD = 0.6
_DEFAULT_PROPOSAL_TTL = timedelta(days=7)
_GENERATOR_NAME = "deterministic-v1"
_GENERATOR_VERSION = "canonicalization-generator-v1"


class _UnionFind:
    """Tiny union-find for clustering similar concept ids."""

    def __init__(self, items: Iterable[str]) -> None:
        self._parent: dict[str, str] = {item: item for item in items}

    def find(self, x: str) -> str:
        # Path compression
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        cur = x
        while self._parent[cur] != root:
            self._parent[cur], cur = root, self._parent[cur]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def groups(self) -> list[list[str]]:
        clusters: dict[str, list[str]] = {}
        for item in self._parent:
            clusters.setdefault(self.find(item), []).append(item)
        return [sorted(g) for g in clusters.values()]


class DeterministicProposer:
    """Generate `merge-concepts` proposals from active-concept similarity."""

    _strategy_name: str = "deterministic"

    def __init__(
        self,
        index: CanonicalizationIndex,
        store: NoteStore,
        clock: Callable[[], datetime] | None = None,
        threshold: float = _DEFAULT_THRESHOLD,
        index_reader: Any | None = None,
    ) -> None:
        self._index = index
        self._store = store
        self._clock = clock or datetime.now
        self._threshold = threshold
        # Optional ``IndexReader`` (Protocol from bsage.garden.index_reader).
        # When wired, ``_garden_tag_frequency`` uses cached NoteSummary
        # tags instead of re-reading every garden file. Tests that don't
        # care about perf can leave it None — the fallback scans storage.
        self._index_reader = index_reader

    # -------------------------------------------------------------- API

    async def generate(self) -> list[str]:
        concepts = await self._index.list_active_concepts()
        if len(concepts) < 2:
            return []

        clusters = await self._collect_clusters(concepts)

        # Pre-compute frequency: canonical selection prefers higher garden usage
        usage = await self._garden_tag_frequency([c.concept_id for c in concepts])
        existing_proposals = await self._existing_pending_merge_signatures()

        created: list[str] = []
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            ids = sorted(cluster)
            canonical = self._pick_canonical(ids, usage)
            merge = [c for c in ids if c != canonical]
            signature = self._cluster_signature(canonical, merge)
            if signature in existing_proposals:
                logger.debug(
                    "proposer_dedup",
                    strategy=self._strategy_name,
                    canonical=canonical,
                    merge=merge,
                )
                continue
            proposal_path = await self._write_proposal(canonical, merge, usage)
            created.append(proposal_path)
            existing_proposals.add(signature)
        return created

    # ------------------------------------------------ extension hooks

    async def _collect_clusters(self, concepts: list[models.ConceptEntry]) -> list[list[str]]:
        """Return clusters of concept ids to consider for merging.

        Overridable: ``BalancedProposer`` augments this with embedding KNN
        candidates and filters by cannot-link decisions.
        """
        return self._cluster_by_similarity(concepts, self._threshold)

    async def _evidence_for_cluster(
        self,
        canonical: str,
        merge: list[str],
        usage: dict[str, int],
    ) -> list[dict[str, Any]]:
        """Build the evidence list attached to the proposal.

        Overridable: ``BalancedProposer`` adds ``embedding_knn`` and
        ``llm_verify`` envelopes (source=model) on top of the deterministic
        ``alias_exact`` + ``frequency`` items.
        """
        now = self._clock()
        evidence: list[dict[str, Any]] = []
        for old_id in merge:
            evidence.append(
                _envelope(
                    kind="alias_exact",
                    schema_version="alias-exact-v1",
                    payload={
                        "alias": old_id,
                        "matches_canonical": canonical,
                        "garden_uses_old": usage.get(old_id, 0),
                    },
                    observed_at=now,
                )
            )
        evidence.append(
            _envelope(
                kind="frequency",
                schema_version="frequency-v1",
                payload={
                    "canonical": canonical,
                    "uses": usage.get(canonical, 0),
                    "merge_uses": {old: usage.get(old, 0) for old in merge},
                },
                observed_at=now,
            )
        )
        return evidence

    # --------------------------------------------------- similarity helpers

    @staticmethod
    def ngram_jaccard(a: str, b: str, *, n: int = _NGRAM_N) -> float:
        if a == b:
            return 1.0
        ga = _ngrams(a, n)
        gb = _ngrams(b, n)
        if not ga and not gb:
            return 0.0
        union = ga | gb
        if not union:
            return 0.0
        return len(ga & gb) / len(union)

    def _cluster_by_similarity(
        self, concepts: list[models.ConceptEntry], threshold: float
    ) -> list[list[str]]:
        ids = [c.concept_id for c in concepts]
        uf = _UnionFind(ids)
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                if self.ngram_jaccard(a, b) >= threshold:
                    uf.union(a, b)
        return [g for g in uf.groups() if len(g) > 1]

    @staticmethod
    def _pick_canonical(ids: list[str], usage: dict[str, int]) -> str:
        # Highest garden frequency wins; ties broken by longest id then alphabetic
        return max(ids, key=lambda c: (usage.get(c, 0), len(c), c))

    @staticmethod
    def _cluster_signature(canonical: str, merge: list[str]) -> str:
        return f"{canonical}|{','.join(sorted(merge))}"

    # ---------------------------------------------------- vault helpers

    async def _garden_tag_frequency(self, concept_ids: list[str]) -> dict[str, int]:
        """Count active concept-id usage across garden notes.

        Fast path: when an ``IndexReader`` is wired (gateway boot), use
        the in-memory ``NoteSummary`` cache — O(N) over summaries, no
        disk IO. Fallback (tests, CLI without gateway): scan storage.
        """
        counts: dict[str, int] = {cid: 0 for cid in concept_ids}
        if self._index_reader is not None:
            try:
                summaries = await self._index_reader.get_all_summaries()
            except Exception as exc:  # noqa: BLE001 — never abort proposer
                logger.warning("proposer_index_reader_failed", error=str(exc))
                summaries = None
            if summaries is not None:
                for s in summaries:
                    if not s.path.startswith("garden/"):
                        continue
                    for tag in s.tags or []:
                        if tag in counts:
                            counts[tag] += 1
                return counts
        # Fallback: re-read every garden note from disk
        for path in await self._store.list_garden_paths():
            tags = await self._store.read_garden_tags(path)
            for tag in tags:
                if tag in counts:
                    counts[tag] += 1
        return counts

    async def _existing_pending_merge_signatures(self) -> set[str]:
        # Look at currently pending merge-concepts proposals; signature is
        # (canonical, merge-list) derived from the linked action draft.
        pending = await self._index.list_proposals(status="pending", kind="merge-concepts")
        signatures: set[str] = set()
        for prop in pending:
            for action_path in prop.action_drafts:
                action = await self._store.read_action(action_path)
                if action is None or action.status not in {"draft", "pending_approval"}:
                    continue
                canonical = action.params.get("canonical")
                merge = action.params.get("merge") or []
                if isinstance(canonical, str) and isinstance(merge, list):
                    signatures.add(self._cluster_signature(canonical, list(merge)))
        return signatures

    async def _write_proposal(
        self,
        canonical: str,
        merge: list[str],
        usage: dict[str, int],
    ) -> str:
        now = self._clock()
        evidence = await self._evidence_for_cluster(canonical, merge, usage)

        # Create paired action draft via the existing draft pathway. The
        # service path handles collision suffix + index invalidation.
        action_path = await self._create_merge_action_draft(canonical, merge, now)

        # Create the proposal note
        proposal_slug = canonical
        candidate = paths.build_proposal_path("merge-concepts", now, proposal_slug)
        existing = await self._store.list_existing_proposal_paths("merge-concepts")
        proposal_path = paths.with_collision_suffix(candidate, existing)

        proposal_score = _confidence_from_jaccard(
            max(self.ngram_jaccard(canonical, m) for m in merge)
        )

        entry = models.ProposalEntry(
            path=proposal_path,
            kind="merge-concepts",
            status="pending",
            strategy=self._strategy_name,
            generator=_GENERATOR_NAME,
            generator_version=_GENERATOR_VERSION,
            proposal_score=proposal_score,
            created_at=now,
            updated_at=now,
            expires_at=now + _DEFAULT_PROPOSAL_TTL,
            freshness={},
            evidence=evidence,
            affected_paths=[],
            action_drafts=[action_path],
            result_actions=[],
        )
        await self._store.write_proposal(entry)
        await self._index.invalidate(proposal_path)
        return proposal_path

    async def _create_merge_action_draft(
        self, canonical: str, merge: list[str], now: datetime
    ) -> str:
        """Write a draft action note directly without going through the service.

        DeterministicProposer is allowed to create draft action notes
        (Handoff §12 explicit), but apply MUST stay with the service. We
        write through the same NoteStore + paths conventions to preserve
        invariants without circular dependency on the service.
        """
        candidate = paths.build_action_path("merge-concepts", now, canonical)
        existing = await self._store.list_existing_action_paths("merge-concepts")
        action_path = paths.with_collision_suffix(candidate, existing)

        entry = models.ActionEntry(
            path=action_path,
            kind="merge-concepts",
            status="draft",
            action_schema_version="merge-concepts-v1",
            params={"canonical": canonical, "merge": list(merge)},
            created_at=now,
            updated_at=now,
            expires_at=now + _DEFAULT_PROPOSAL_TTL,
        )
        await self._store.write_action(entry)
        await self._index.invalidate(action_path)
        return action_path


# ---------------------------------------------------------------- utils


def _ngrams(s: str, n: int) -> set[str]:
    if not s:
        return set()
    if len(s) < n:
        return {s}
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def _confidence_from_jaccard(j: float) -> float:
    """Map a 0..1 Jaccard similarity into a proposal score with mild bias."""
    return round(min(0.99, max(0.0, j)), 3)


def _envelope(
    *,
    kind: str,
    schema_version: str,
    payload: dict[str, Any],
    observed_at: datetime,
    source: str = "deterministic",
    producer: str = _GENERATOR_NAME,
) -> dict[str, Any]:
    """Thin wrapper over evidence.envelope() — kept positional-arg
    compatible with the in-module call sites (alias_exact / frequency /
    embedding_knn / llm_verify)."""
    return _evidence_envelope(
        kind=kind,
        schema_version=schema_version,
        payload=payload,
        source=source,
        producer=producer,
        observed_at=observed_at,
    )


# ============================================================================
# BalancedProposer (Handoff §11 balanced strategy + §12)
# ============================================================================

# Type aliases — kept loose so both sync mocks and real async clients fit.
EmbedderFn = Any  # async (list[str]) -> list[list[float]]
VerifierFn = Any  # async (a, b) -> {"verdict": str, "confidence": float}

_BALANCED_GENERATOR = "balanced-v1"
_DEFAULT_EMBEDDING_TOP_K = 5
_DEFAULT_EMBEDDING_THRESHOLD = 0.85
_DEFAULT_CANNOT_LINK_THRESHOLD = 0.85


class BalancedProposer(DeterministicProposer):
    """Deterministic preprocessing + embedding KNN + selective LLM verify.

    Per Handoff §12 — ``balanced`` is the default first-implementation
    strategy. Slice 4 ships the schema/wiring with pluggable callables;
    slice 5 wires real Embedder + LiteLLM clients at gateway boot.

    Constructor accepts:
    - ``embedder``: async ``(list[str]) -> list[list[float]]``. None disables
      embedding KNN augmentation.
    - ``verifier``: async ``(a, b) -> {verdict, confidence}``. None disables
      LLM verify evidence.
    - ``decisions``: ``DecisionMemory``. When set, candidate pairs whose
      effective cannot-link strength is at/above ``cannot_link_threshold``
      are dropped before proposal generation (proposer-stage suppression).
    """

    _strategy_name = "balanced"

    def __init__(
        self,
        index: CanonicalizationIndex,
        store: NoteStore,
        clock: Callable[[], datetime] | None = None,
        threshold: float = _DEFAULT_THRESHOLD,
        embedder: EmbedderFn | None = None,
        verifier: VerifierFn | None = None,
        decisions: Any | None = None,  # DecisionMemory; Any to avoid import cycle
        embedding_top_k: int = _DEFAULT_EMBEDDING_TOP_K,
        embedding_threshold: float = _DEFAULT_EMBEDDING_THRESHOLD,
        cannot_link_threshold: float = _DEFAULT_CANNOT_LINK_THRESHOLD,
        index_reader: Any | None = None,
    ) -> None:
        super().__init__(
            index=index,
            store=store,
            clock=clock,
            threshold=threshold,
            index_reader=index_reader,
        )
        self._embedder = embedder
        self._verifier = verifier
        self._decisions = decisions
        self._embedding_top_k = embedding_top_k
        self._embedding_threshold = embedding_threshold
        self._cannot_link_threshold = cannot_link_threshold
        self._embedding_pairs: dict[tuple[str, str], float] = {}

    # ----------------------------------------------- cluster collection

    async def _collect_clusters(self, concepts: list[models.ConceptEntry]) -> list[list[str]]:
        clusters = await super()._collect_clusters(concepts)

        # Optional embedding-KNN augmentation
        if self._embedder is not None:
            extra_pairs = await self._embedding_neighbor_pairs(concepts)
            if extra_pairs:
                ids = [c.concept_id for c in concepts]
                uf = _UnionFind(ids)
                # Seed with deterministic clusters so they merge with KNN edges
                for group in clusters:
                    for i in range(len(group) - 1):
                        uf.union(group[i], group[i + 1])
                for (a, b), _cosine in extra_pairs.items():
                    uf.union(a, b)
                clusters = [g for g in uf.groups() if len(g) > 1]

        # Optional cannot-link suppression
        if self._decisions is not None:
            clusters = await self._filter_by_cannot_link(clusters)
        return clusters

    async def _embedding_neighbor_pairs(
        self, concepts: list[models.ConceptEntry]
    ) -> dict[tuple[str, str], float]:
        """Compute cosine-similar pairs above threshold via the embedder.

        Result is cached on ``self._embedding_pairs`` so the evidence hook
        can decorate proposals without re-embedding.
        """
        if self._embedder is None:
            return {}
        ids = [c.concept_id for c in concepts]
        try:
            vectors = await self._embedder(ids)
        except Exception as exc:  # noqa: BLE001 — never abort proposer on embed error
            logger.warning("balanced_proposer_embed_failed", error=str(exc))
            return {}
        if len(vectors) != len(ids):
            logger.warning(
                "balanced_proposer_embed_shape_mismatch",
                expected=len(ids),
                got=len(vectors),
            )
            return {}
        # Reuse the canonical cosine impl from vector_store rather than
        # duplicating it (zero-norm guard included).
        pairs: dict[tuple[str, str], float] = {}
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                cos = _cosine_similarity(vectors[i], vectors[j])
                if cos >= self._embedding_threshold:
                    key = tuple(sorted((ids[i], ids[j])))
                    pairs[key] = cos
        self._embedding_pairs = pairs
        return pairs

    async def _filter_by_cannot_link(self, clusters: list[list[str]]) -> list[list[str]]:
        """Drop entire clusters where any pair hits the cannot-link threshold."""
        if self._decisions is None:
            return clusters
        kept: list[list[str]] = []
        for cluster in clusters:
            blocked = False
            for i, a in enumerate(cluster):
                for b in cluster[i + 1 :]:
                    strength = await self._decisions.max_cannot_link_strength(
                        (a, b), now=self._clock()
                    )
                    if strength >= self._cannot_link_threshold:
                        logger.debug(
                            "balanced_proposer_cluster_dropped",
                            pair=(a, b),
                            cannot_link_strength=strength,
                        )
                        blocked = True
                        break
                if blocked:
                    break
            if not blocked:
                kept.append(cluster)
        return kept

    # ------------------------------------------------- evidence layer

    async def _evidence_for_cluster(
        self, canonical: str, merge: list[str], usage: dict[str, int]
    ) -> list[dict[str, Any]]:
        evidence = await super()._evidence_for_cluster(canonical, merge, usage)
        now = self._clock()

        # Embedding KNN evidence (source=model — embedding output is model-derived)
        for old_id in merge:
            key = tuple(sorted((canonical, old_id)))
            cosine = self._embedding_pairs.get(key)
            if cosine is None:
                continue
            evidence.append(
                _envelope(
                    kind="embedding_knn",
                    schema_version="embedding-knn-v1",
                    payload={
                        "pair": list(key),
                        "cosine": round(float(cosine), 4),
                        "top_k": self._embedding_top_k,
                        "threshold": self._embedding_threshold,
                    },
                    observed_at=now,
                    source="model",
                    producer=_BALANCED_GENERATOR,
                )
            )

        # LLM verify evidence (source=model)
        if self._verifier is not None:
            for old_id in merge:
                try:
                    verdict = await self._verifier(canonical, old_id)
                except Exception as exc:  # noqa: BLE001 — verify failure logged, not fatal
                    logger.warning(
                        "balanced_proposer_verify_failed",
                        pair=(canonical, old_id),
                        error=str(exc),
                    )
                    continue
                if not isinstance(verdict, dict):
                    continue
                evidence.append(
                    _envelope(
                        kind="llm_verify",
                        schema_version="llm-verify-v1",
                        payload={
                            "pair": [canonical, old_id],
                            "verdict": verdict.get("verdict"),
                            "confidence": verdict.get("confidence"),
                            "explanation": verdict.get("explanation"),
                        },
                        observed_at=now,
                        source="model",
                        producer=_BALANCED_GENERATOR,
                    )
                )
        return evidence
