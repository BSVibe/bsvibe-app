# E2E Checklist — Concept graph honors retraction tombstones

Fixes: the PWA knowledge graph (`GET /api/v1/inside/graph` → `build_concept_graph`)
kept rendering concept/entity nodes for concepts whose `concepts/active/*.md` files
carry a `retracted_at` tombstone — the graph was a separate materialization that
ignored the marker the RAG retriever already honors.

## Node filtering

- [x] A retracted active concept does not become a graph node —
      `test_retracted_concept_excluded_from_nodes`.
- [x] A retracted concept forms no edges, even when its id still appears in a live
      observation's tags (no `add_edge` implicit re-introduction) —
      `test_retracted_concept_forms_no_edges`.
- [x] `ConceptEntry.retracted_at` is additive/optional; shared `read_concept` still
      returns the entry (canonicalization merge/resolve invariants unchanged).

## Edge filtering

- [x] A retracted garden observation contributes no co-occurrence edge —
      `test_retracted_observation_excluded_from_cooccurrence` (live obs → weight 1.0,
      not 2.0).
- [x] `read_garden_frontmatter` is the single source for tags + `retracted_at` so the
      two never drift; `read_garden_tags` delegates to it.
- [x] Falsy `retracted_at` ('' / None) treated as live (half-written note fails open),
      matching `ResolvedDecisionsRetriever`.

## No cache / rebuild needed

- [x] Graph is built fresh per request (no cache) — deploying the fix immediately
      stops retracted nodes rendering; no index backfill required.

## Regression guard

- [x] tests/knowledge + tests/api inside-graph + concept-detail green (996 passed).
- [x] `ruff check` + `ruff format --check` clean on touched files.
