# CI parity and product-declared checks — E2E checklist

Scope: every gate command runs with `CI=true`; a product's `verify_floor` runs on every
verification and its `verify_final` once after everything else passed; the deriver and the
agent are told to keep tests and CI checks unnarrowed and on CI's pinned tool versions.

Unit and wiring coverage: `tests/workflow/test_verify_ci_parity.py` (12 wire-cuts, each one
red when its production edge is removed).

## A. Declaration

- [x] `bsvibe_products_set_metadata` with `verify_floor: "x"` (a string) is refused with a
      message naming the key.
- [x] Setting a list for `verify_floor` / `verify_final` on bsvibe-app is accepted, and
      `bsvibe_products_show` returns it unchanged (secrets still masked, not wiped).

## B. A live run on bsvibe-app

- [x] `runs_detail` for the next run's verification shows `floor` commands in
      `derived_gate.commands` on every round.
- [x] The passing round shows `final` commands, and no failed round does.
- [x] A round whose `final` fails goes back to the agent with the failing command in the
      feedback (not a silent pass, not a founder question).
      (#1332, #1336, #1339 round 1: `GATE COMMAND FAILED: uv run pytest -q` in the feedback; the next round fixed the broken test.)
- [x] No round fails on `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` (CI=true reached the
      command).

## C. Parked #1204 re-run

- [x] The derived pnpm commands use the CI-pinned version (`pnpm@10.33.0`), not `pnpm@10`.
- [ ] The run passes in one round, or its failure names a real defect in the change.
      (2026-10-27, run d8ae99bd: round 1 failed only because no `requirement_evidence` was
      declared — all 16 checks passed; round 2 passed with floor + final. Section E closes it.)

## D. client_attach (BStockReport)

- [x] A run with no `verify_floor` / `verify_final` declared behaves as before (no extra
      commands in the in-place gate blob).
      (BStockReport 11-09 `d330b009`: one derived check only, no floor/final.)

## E. Before a round is spent

- [ ] A run that says "done" with owed ledger items unevidenced gets ONE
      `REQUIREMENT EVIDENCE MISSING` turn naming them, and its first verification carries the
      evidence.
- [x] The derived gate no longer repeats a command the product's floor or final runs (no second
      full pytest in the same verification).
      (#1339 run `f83bb33b`: derived `pytest` scoped to `tests/delivery …`, the full `pytest -q` once, as `final`.)
- [ ] A ledger item that names an identifier deep in a long diff is judged `done` or
      `rejected`, not `unclear` from a cut-off excerpt.

## F. Quick checks before a round (`verify_precheck`)

- [ ] With bsvibe-app's `verify_precheck` set, a run whose change breaks mypy gets a
      `QUICK CHECKS FAILED` turn naming the error, and its first verification passes the final `mypy`.
- [ ] A run whose quick checks pass sees no such turn (no added round, ~1.5 min added once).
