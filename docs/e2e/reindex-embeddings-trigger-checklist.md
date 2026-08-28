# E2E — the embedding backfill has a trigger a founder can press

The vector index is maintained event-driven by the settle promote hook, so it
only heals when knowledge activity happens to occur. Measured on prod
2026-08-28: `note_embeddings` held **1,724 rows with `content_hash` NULL** and
**zero runs in the 30 hours since #838 deployed** — the backfill was correct and
had never had an opportunity. Its only deliberate trigger was
`POST /api/v1/inside/reindex-embeddings`, whose callers across the whole repo
were tests: no MCP tool, no PWA control, no SDK method.

This checklist proves the two new surfaces reach the same backfill, and that the
backfill actually moves the number it is supposed to move.

## Baseline (record BEFORE firing anything)

- [ ] `select count(*) total, count(content_hash) with_hash from note_embeddings
      group by workspace_id;` — record both numbers per workspace
- [ ] `select max(updated_at) from note_embeddings;` — record
- [ ] Confirm the deployment has an embedding model (`disabled` must come back
      `false`; a `true` here means the run below proves nothing)

## MCP surface

- [ ] `bsvibe_knowledge_reindex_embeddings` appears in the MCP tool list
- [ ] Calling it with an `mcp:read`-only principal is refused (scope denied)
- [ ] Calling it returns real counts — `scanned` matches the workspace's note
      count on disk (`garden` + `concepts`), not `0`
- [ ] `with_hash` in the baseline query has risen by `embedded`
- [ ] Calling it a SECOND time returns `embedded: 0` and `already == scanned`
      (idempotent — the fingerprint is unchanged)

## PWA surface

- [ ] Settings → Developer shows a "Search index" section with a "Rebuild index"
      button
- [ ] Pressing it disables the button and shows "Rebuilding…" while in flight
- [ ] On completion it reports the three real counts, not a generic "done"
- [ ] The counts it reports match a direct DB read of the same pass
- [ ] Korean locale renders the section (no raw message keys)

## Honesty of the report

- [ ] With no embedding model configured, the surfaces say so explicitly —
      they must NOT render `0 scanned` as if the corpus were clean
- [ ] A failing pass surfaces the error; it must not look finished

## Region boundary

- [ ] The vault read is the workspace's OWN region (the boundary the settle hook
      writes through), not `knowledge_default_region`. On a deployment where the
      two differ, resolving the default reads an empty directory and reports
      `scanned: 0` as a success.
      ⚠️ Not observable on prod today — all three workspaces are `us-1`, which
      IS the default. Covered by unit test instead
      (`test_reindex_tool_reads_the_workspace_region_not_the_default`).
