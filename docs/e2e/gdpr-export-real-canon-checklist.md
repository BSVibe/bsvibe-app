# E2E — GDPR export carries the founder's REAL canonical knowledge (B6)

The Art. 15 / 20 data export (`GET /api/v1/workspace/export`) read the
workspace's canonical knowledge from `SqlAlchemyCanonicalAnchorRepository`
— the `canonical_anchors` DB table. That table is **producer-less**: nothing in
the app ever writes it. So `knowledge_concepts` was `[]` for EVERY workspace,
forever — the export silently under-reported the founder's data (a regulatory
defect).

The founder's real canonical knowledge lives in the per-workspace vault at
`concepts/active/<id>.md` — the same FS-as-SoT `GET /inside/concepts` and the
PWA knowledge graph render. This lift swaps the export's canon source to that
vault reader (`build_inside_index` / `build_inside_storage`). The rest of the
export is unchanged.

## Backend — `_build_export` data source

- [ ] `knowledge_concepts` is read from the per-workspace vault
      (`concepts/active/*.md`) via `build_inside_index` /
      `build_inside_storage`, NOT `SqlAlchemyCanonicalAnchorRepository`.
- [ ] The import + instantiation of the `canonical_anchors` repository is gone
      from `backend/api/v1/workspace_compliance.py`.
- [ ] Each exported concept carries `id` (concept id), `name` (display H1),
      `type` (note kind), `aliases`, `description` (full note body,
      frontmatter stripped — portable per Art. 20), `created_at`, `updated_at`.
- [ ] Concepts are ordered newest-settled first (`updated_at` descending),
      mirroring `GET /inside/concepts`.

## Backend — REST (`GET /api/v1/workspace/export`)

- [ ] A workspace whose vault holds active concepts exports them under
      `knowledge_concepts` (round-trips the vault ids).
- [ ] A workspace with an empty vault exports `knowledge_concepts == []` — not
      an error, and not a stale DB read.
- [ ] The concepts are workspace-scoped: another workspace's vault concepts are
      never enumerated (the reader roots at
      `<vault_root>/<region>/<workspace_id>/`).
- [ ] The overall export contract is unchanged: `profile`, `workspace`,
      `products`, `product_resources`, `resource_bindings`, `requests`, `runs`,
      `deliverables`, `decisions`, `knowledge_concepts`, `exported_at`,
      `schema_version` all still present.

## Live verification (prod / staging)

- [ ] Pick a workspace whose Inside view shows N active concepts. `GET
      /api/v1/workspace/export` returns `len(knowledge_concepts) == N` with the
      same ids the Inside concept list shows.
- [ ] The exported concept bodies match the vault note bodies (portability, not
      a preview).

## Regression guardrails

- [ ] B5 retraction changes, the two-role DB, and verification are untouched.
- [ ] `GET /api/v1/workspace/processing-record` (Art. 30) is unchanged.
- [ ] Phase-4 note: `canonical_anchors` (and the sibling
      `canonicalization_{proposals,decisions,policies}` tables) are now fully
      producer-less AND reader-less in production — dead schema, safe to drop in
      a separate migration.
