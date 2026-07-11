# E2E Checklist — Unified Routing Lift 4 (PWA single routing page)

The Settings → Models → ROUTING page pointed at the deleted Layer-2 `/api/v1/rules`
(runtime 404 after Lift 2). Lift 4 rebuilds it on the surviving run-routing table
(`/api/v1/run-routing`). The `DefaultAccountPicker` ("기본 모델") was already on
`/api/v1/workspace` and is unchanged.

Verified via the backend run-routing suite + PWA vitest (client wire-contracts +
component behaviour against a mocked fetch).

## Backend
- [x] New `GET /api/v1/run-routing/callers` returns the registry callers (incl.
      `workflow.agent_loop.plan`, `chat.completions`), each with a description —
      `test_list_callers_returns_known_callers`. Keeps the caller whitelist a
      single source of truth (no hardcoded list in the FE).
- [x] Existing run-routing create/list/delete + validation tests still green (9).

## PWA — client (`test/run-routing-client.test.ts`)
- [x] `listRunRoutingRules` → GET /api/v1/run-routing.
- [x] `listRunRoutingCallers` → GET /api/v1/run-routing/callers.
- [x] `createRunRoutingRule` POSTs `{name, caller_id, target, priority, is_default}`
      and drops empty `conditions`.
- [x] `createRunRoutingRule` omits `caller_id` for a catch-all default.
- [x] `deleteRunRoutingRule` → DELETE /api/v1/run-routing/{id} → void.
- [x] Non-ok read surfaces an `ApiError`.

## PWA — component (`test/run-routing-rules.test.tsx`)
- [x] Calm empty state when there are no rules.
- [x] Lists each rule: name, caller → target, chips.
- [x] Add form loads callers + accounts into dropdowns, POSTs the body, re-reads.
- [x] Delete is confirm-gated → DELETE fires → re-read.
- [x] Failed list read degrades to a calm inline note.

## Regression
- [x] Full PWA suite: 698 passed (i18n key parity incl. new ko + en `caller`/
      `callerPlaceholder`). Biome + `tsc --noEmit` clean.
- [x] Old Layer-2 UI (`RoutingRules.tsx`, `lib/api/rules.ts`) + its tests deleted.

## Deferred
- [ ] Condition editor (stage / classified_intent / estimated_tokens) — the form
      authors caller + target only; conditions are still MCP/REST-only.
- [ ] Manual visual pass in the running PWA (Settings → Models → Routing).
