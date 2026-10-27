# CI parity and product-declared checks — E2E checklist

Scope: every gate command runs with `CI=true`; a product's `verify_floor` runs on every
verification and its `verify_final` once after everything else passed; the deriver and the
agent are told to keep tests and CI checks unnarrowed and on CI's pinned tool versions.

Unit and wiring coverage: `tests/workflow/test_verify_ci_parity.py` (12 wire-cuts, each one
red when its production edge is removed).

## A. Declaration

- [ ] `bsvibe_products_set_metadata` with `verify_floor: "x"` (a string) is refused with a
      message naming the key.
- [ ] Setting a list for `verify_floor` / `verify_final` on bsvibe-app is accepted, and
      `bsvibe_products_show` returns it unchanged (secrets still masked, not wiped).

## B. A live run on bsvibe-app

- [ ] `runs_detail` for the next run's verification shows `floor` commands in
      `derived_gate.commands` on every round.
- [ ] The passing round shows `final` commands, and no failed round does.
- [ ] A round whose `final` fails goes back to the agent with the failing command in the
      feedback (not a silent pass, not a founder question).
- [ ] No round fails on `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` (CI=true reached the
      command).

## C. Parked #1204 re-run

- [ ] The derived pnpm commands use the CI-pinned version (`pnpm@10.33.0`), not `pnpm@10`.
- [ ] The run passes in one round, or its failure names a real defect in the change.

## D. client_attach (BStockReport)

- [ ] A run with no `verify_floor` / `verify_final` declared behaves as before (no extra
      commands in the in-place gate blob).
