# E2E — R8 report fixes (4 live-feedback items)

## #1 Referenced knowledge — note-level (no fake links)
- [x] "Related note — garden/seedling/settle-<slug>.md" renders as a readable
      de-slugged title (non-interactive chip), not a raw internal path
- [ ] (follow-up) a real note VIEWER / deep-link needs structured refs (note-id
      threaded through retrieval) — deferred

## #2 Footer mirrors the Brief (state-aware action)
- [x] backend: report response carries run_status + held_delivery_item_id
- [x] a HELD delivery (pending Safe-Mode item) → Approve & ship / Decline on that item
- [x] a SHIPPED run → Rollback; neither otherwise
- [x] approving from the report re-reads the report (footer reflects new state)

## #3 Report button CSS
- [x] the rollback trigger is a clean underline text-link (was a default-bordered
      box — `.report-rollback__open` now resets button chrome)
- [x] confirm panel + cancel/danger use defined tokens (var(--line) → --hair-strong)

## #4 Decisions nav tab removed
- [x] no "Decisions" tab in the desktop left rail or the mobile tab bar
- [x] /decisions route still resolves by URL (not deleted)
- [ ] (open) knowledge/canon proposals currently live ONLY on /decisions — needs
      a home in the Brief or Knowledge tab (founder decision)

## Gates
- [x] frontend: biome + tsc + vitest (632) + next build; impeccable detect 0
- [x] backend: ruff + mypy + pytest (48 deliverables) ; D35 LOC held
