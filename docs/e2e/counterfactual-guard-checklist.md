# Counterfactual guard check (R4) — E2E checklist

Scope: for each ledger `guard` item judged `done` on cited tests, verification puts the run's
non-test changes back to the baseline, runs the cited test alone, restores the tree, and marks
the row `holds` / `vacuous` / `unknown`. Report-only (founder 2026-10-28): the verdict never
changes because of it.

Unit and wiring coverage: `tests/execution/test_counterfactual_guard.py` (real git, real shell;
11 wire-cuts, each red when its production edge is removed).

## A. A live run with a guard item

- [ ] The run's verification log has `counterfactual_checked` with a result per guard item.
- [ ] The deliverable report's requirement rows carry `counterfactual` and its note.
- [ ] A `vacuous` guard shows "⚠ passes without the change" in the PR body's ledger table.
- [ ] The run's final check still passes after the check — the tree was restored
      (no `counterfactual_restore_incomplete` in the worker log).

## B. Cost

- [ ] The check adds at most one test run per guard item (≤ 5) and one model call each;
      record the added seconds from the verification timing.

## C. Before turning it into a gate

- [ ] Over several runs, count `vacuous` findings and how many were legitimate regression
      tests of behaviour that already existed. Decide gate vs report with that number (R6).
