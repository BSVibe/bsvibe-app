# E2E Checklist — Unified Routing Lift 5 (NL rule authoring)

The founder describes routing in plain language ("설계는 opus, 나머지는 sonnet") and one
cheap LLM call compiles it into structured, VALIDATED run-routing rule proposals
(dry-run — nothing persists until applied). Full stack: compile service + REST +
MCP + PWA. The LLM is stubbed/mocked everywhere — no real API in tests.

## Backend — compiler service (`tests/router/routing/run_routing/test_nl_compile.py`)
- [x] Compiles "design → opus, rest → sonnet" into a plan rule + a default rule.
- [x] The prompt carries the caller + model catalogs.
- [x] Drops a hallucinated caller_id or a target not in the workspace's accounts.
- [x] `is_default` forces caller_id null; only one default kept.
- [x] A non-default rule with no known caller is dropped.
- [x] Priority coerced + floored; code-fence tolerated.
- [x] Empty text / no targets / LLM failure / unparseable → `[]` (never raises).
- [x] `compile_for_workspace` builds the model catalog from ACTIVE accounts and
      returns create-shaped proposals (end-to-end with a seeded account).

## Backend — REST (`tests/api/test_v1_run_routing.py`)
- [x] `POST /api/v1/run-routing/compile` returns proposals (dry-run — a follow-up
      `GET` shows nothing was created).
- [x] No model configured → 400 (`NoCompileModelError` → helpful detail).

## Backend — MCP (`tests/mcp/test_run_routing_rules_tools.py`)
- [x] `bsvibe_run_routing_rules_compile` returns proposals via the shared helper
      (mcp:read; dry-run). Parity with the REST surface.

## PWA (`test/run-routing-client.test.ts`, `test/run-routing-rules.test.tsx`)
- [x] `compileRunRoutingRules` POSTs `/api/v1/run-routing/compile` with `{text}`.
- [x] "✨ Describe in words" → type → "Draft rules" previews the proposals; nothing
      is created yet. "Apply all" then creates each via the create endpoint.
- [x] i18n: NL keys added (en + ko); full PWA suite 700 passed, biome + tsc clean.

## Regression
- [x] Backend targeted regression 372 passed (dispatch / api / mcp / router /
      import-contracts / dunder-all / docstring-contract). The new
      `routing.compile` caller is registered and appears in `/callers`.

## Deferred
- [ ] Streaming / per-proposal edit before apply (apply is all-or-nothing today).
- [ ] Manual visual pass in the running PWA.
