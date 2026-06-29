# E2E Checklist — KG Redesign Lift 5: PWA local-graph view

Surface: Knowledge graph (`app.bsvibe.dev` → Knowledge). Backend unchanged — the
local graph is built purely from the `getConceptDetail(id)` data the inspector
already fetches.

Automated coverage (vitest + RTL, CI-gated, force-graph lib stubbed since jsdom
has no canvas):
- `test/local-graph.test.ts` — `buildLocalGraph` shape (focus + related + seedling, edge types, dedupe, lone concept).
- `test/knowledge-localgraph.test.tsx` — global overview default, select→local switch, full-graph return, canvas pivot on related concept, seedling-leaf is not a pivot/404.

Live prod verification (Playwright MCP browser, ws `5fa3494c` = 437 concepts + 825 embeddings):

- [ ] Knowledge surface loads the global concept overview (concept-only, no seedlings flat).
- [ ] Clicking a concept opens the inspector AND switches the canvas to that concept's local graph (`data-view-mode="local"`).
- [ ] The local graph shows the focus concept + its related concepts + its member observations as seedling leaves (1-hop).
- [ ] The "← Full graph" control returns to the global overview (`data-view-mode="global"`).
- [ ] Clicking a related concept node in the local canvas pivots the focus (re-fetches + re-centres).
- [ ] Clicking a seedling leaf does NOT 404 / does NOT pivot (its body already shows in the inspector Content).
- [ ] Light + dark themes both read cleanly (seedling leaves muted, concept hubs coloured).
