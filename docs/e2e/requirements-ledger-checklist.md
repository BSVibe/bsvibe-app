# Requirements ledger (#1244, v1) — live checklist

Verified after deploy, on the production stack, through BSVibe's own MCP tools and
`docker --context colima logs` (read-only). No production DB reads.

v1 scope: a **code run verified on the server** whose request **structures** what it wants
(unticked checkboxes, or a list under 할 것 / 가드 / 완료 기준 / 원하는 동작 / 범위 밖 /
Guards / Acceptance …). Everything else is unchanged.

## A. A code run from an issue
- [ ] File an issue with a `## 할 것` checklist and a `## 가드` list → the run's first turn shows
      the `REQUIREMENTS LEDGER` block with `R1…Rn`.
- [x] The deliverable report (`bsvibe_deliverables_report`) has `requirements` rows (one per item:
      `done` / `deferred` / `unclear` / `noted`) and `ledger_counts`.
      (2026-11-06, #1334 deliverable `0af64718`: three `requirements` rows, `ledger_counts` 3/3 done.)
- [x] The PR body has a `## Requirements ledger` table; with every owed item done it ends with
      `Closes #N`.
      (PR #1328 `Closes #1327`, PR #1335 `Closes #1334`, PR #1337 `Closes #1336`.)

## B. A run started by a comment on an issue
- [ ] Comment (as the repo owner) on an existing issue whose body has the checklist → the ledger
      holds the issue body's items (source `issue`) and the comment's items (source `comment`).

## C. The gate holds the run to it
- [x] An item left without evidence fails verification, and the next round's feedback names it
      (`REQUIREMENTS NOT SHOWN — …`).
      (2026-11-05, #1316 run `26701cf3` round 1: `REQUIREMENTS NOT SHOWN — R5 …`; round 2 passed.)
- [ ] A deferred item (with a reason) still passes, and the PR body says `Refs #N`, not
      `Closes #N`.

## D. Where v1 does not apply (must be unchanged)
- [ ] A request with no structure ("make it nicer") gets no ledger and verifies as before.
- [ ] The weekly BStockReport run (client_attach, direct_output) gets no ledger and still ships
      with one Telegram message.
      (No ledger: confirmed on 11-02 `f03aa42b` and 11-09 `d330b009`. Ships: FAILED both weeks — the
      report reached Telegram but a spurious `delivery_failed` (no PR for a non-code artifact, #1266)
      held the run; fixed by #1339 / PR #1340. Re-check on the 11-16 run.)
- [ ] A run framed before the deploy finishes as before.

## E. Nothing leaks
- [ ] No token / secret appears in the ledger table, the report or the PR body.
