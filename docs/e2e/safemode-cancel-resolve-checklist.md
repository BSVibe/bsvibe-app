# E2E — cancel resolves a run's pending safe_mode items (orphaned-half)

Parallel to #658 (which cascade-resolved a cancelled run's pending *decisions*):
a cancelled run's PENDING `safe_mode_queue_items` were left pending forever, so
the founder's approval queue accumulated dead cards (measured: 6 on cancelled +
16 on shipped runs in prod, 2026-07-31). This fix denies a run's pending items on
the CANCEL paths.

## Covered (this change)
- [x] `cancel_run` (MCP `bsvibe_runs_cancel` + REST `POST /runs/{id}/cancel`) denies the run's pending safe_mode items; surfaces `safe_mode_items_resolved`.
- [x] `discard_run` (MCP `bsvibe_runs_discard`) denies them too.
- [x] `cancel_product_runs` (product-delete cascade) denies them for every cancelled run.
- [x] A non-cancellable run (review_ready via `cancel_run`) leaves its item PENDING (no false deny).

## Live check (prod)
- [ ] Create a run that produces a deliverable → pending safe_mode item; cancel the run (PWA stop / MCP `bsvibe_runs_cancel`); confirm the item leaves the founder approval queue (status `denied`).
- [ ] Delete a product with an in-flight run that has a pending item; confirm the item is denied (cascade).

## Deferred (follow-up — NOT in this change)
- [ ] **SHIPPED-run orphans** (16 observed): when a multi-deliverable run ships on
      one deliverable, a leftover pending item stays orphaned. SHIPPED is set in
      3+ funnels (agent_runner `_auto_ship_product_run` direct-set + `transition`,
      `run_delivery_resolution._record_hop`, `checkpoint_resolution._ship_decision_run`)
      — no single terminal seam. Resolving on ship needs either a consolidated
      terminal-transition seam or a reaper sweep (deny pending items whose run is
      terminal). Kept out of this PR to avoid partial/inconsistent coverage +
      the semantic question of auto-denying a deliverable the founder might still
      want. The 22 cancelled/shipped orphans were already cleared manually.
