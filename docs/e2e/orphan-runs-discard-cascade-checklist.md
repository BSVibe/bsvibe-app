# E2E Checklist — Orphaned-run cleanup: discard tools + product-delete cascade

Fixes: deleting a product orphaned its ExecutionRuns (`product_id` is a loose
reference, no FK cascade), and there was no MCP/API path to clear a
`review_ready` run that had no Safe Mode entry (the "답변이 필요해요" dashboard
entries). REST `/runs/{id}/cancel` only accepts OPEN/RUNNING.

## Product-delete cascade

- [x] Deleting a product cancels its non-terminal (open/running/review_ready)
      runs before the hard delete — `test_delete_product_cascade_cancels_runs`,
      `test_cancel_product_runs_cancels_non_terminal_only`.
- [x] Terminal runs (shipped/failed/cancelled) are left untouched.
- [x] Another product's runs are untouched (product_id-scoped).
- [x] Each cancel appends an `ExecutionRunHistory` audit row (reason='product deleted').

## bsvibe_runs_cancel (MCP, mirrors REST /cancel)

- [x] Cancels an OPEN/RUNNING run → `cancelled` — `test_runs_cancel_cancels_inflight`.
- [x] Errors on a `review_ready` run and points to `bsvibe_runs_discard` —
      `test_runs_cancel_review_ready_errors`.
- [x] Requires `mcp:write`; unknown/cross-workspace → not found.

## Summary dashboard is driven by pending Decisions (follow-up)

- [x] The Summary "확인 필요" tab lists PENDING `Decision` rows
      (`GET /api/v1/checkpoints`), NOT run status — so cancelling the run alone
      left its "답변이 필요해요" card up. discard + product-cascade now resolve the
      run's pending Decisions (status→RESOLVED, resolution/resolved_at/resolved_by) —
      `test_discard_resolves_pending_decisions`,
      `test_cancel_product_runs_resolves_pending_decisions`.
- [x] Already-resolved Decisions are left untouched —
      `test_discard_already_resolved_decision_untouched`.

## bsvibe_runs_discard (MCP, the 폐기 primitive)

- [x] Transitions any non-terminal run (incl. review_ready) → `cancelled` +
      resolves its pending Decisions (returned in `decisions_resolved`) —
      `test_runs_discard_cancels_review_ready`.
- [x] Tombstones handle-less deliverables (`retracted_at`), returned in
      `deliverables_retracted`.
- [x] Deliverables WITH compensation handles are NOT silently tombstoned — surfaced
      in `deliverables_need_compensation` for an explicit REST/PWA compensating
      retract — `test_discard_surfaces_deliverables_with_compensation_handles`.
- [x] Best-effort worktree removal never fails the discard.
- [x] Requires `mcp:write`; unknown/cross-workspace → not found.

## Architecture

- [x] `run_cleanup` replicates the minimal CANCELLED transition inline (status +
      history) rather than importing `AgentRunner`, so the MCP leaf surface does
      NOT drag the agent-execution engine into its import graph — `lint-imports`
      stays 5 kept / 0 broken (no new exemption).

## Regression guard

- [x] tests/mcp + tests/workflow + glue run-cancel + products + deliverables-retract
      (438 passed, 2 PG-only skips).
- [x] `ruff check` + `ruff format --check` clean across backend/ + tests/.
