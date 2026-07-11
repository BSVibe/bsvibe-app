# E2E Checklist — Unified Routing Lift 6 (routing page UX refinements)

Founder feedback on the shipped routing page. Refinements + a rule-edit feature.

## Backend
- [x] `PATCH /api/v1/run-routing/{id}` edits a rule's caller / target / is_active;
      caller validated against the registry; 404 unknown; 422 bad caller —
      `test_update_rule_changes_caller_and_target`, `test_update_rejects_unknown_caller`,
      `test_update_unknown_rule_404`.
- [x] MCP `bsvibe_run_routing_rules_update` (mcp:write) mirrors PATCH —
      `test_update_rule_changes_caller_and_target` (mcp).
- [x] Backend regression 179 passed; ruff clean.

## PWA
- [x] `updateRunRoutingRule` PATCHes `/api/v1/run-routing/{id}`.
- [x] **Priority removed** from the form + card (auto `priority: 10` on create); the
      lede no longer mentions priority.
- [x] **Default de-duplicated**: `is_default` rules are hidden from the list (the
      "Default model" picker above is the single default surface). An NL default
      proposal (`caller_id: null`) calls `setWorkspaceDefaultAccount` instead of
      creating a hidden rule.
- [x] **"Describe in words"** button: emoji removed; both actions grouped on the
      right (`.routing__actions`), no longer floating in the middle.
- [x] **Card is one line** `caller → friendly model`: the target resolves to the
      account label (fixes the raw account-id that used to show), and the
      duplicated freeform name + caller→target subtitle are gone.
- [x] **Edit**: each card has an Edit button → inline caller/target form → PATCH →
      re-read.
- [x] Add form simplified to caller + target selects (name auto-derived
      `caller → target`).
- [x] Component + client tests updated: friendly-target render, is_default hidden,
      create (no name/priority), edit→PATCH, delete, NL default→picker. PWA suite
      703 passed; biome + tsc clean.

## Live data cleanup (founder workspace, via MCP)
- [x] haiku removed: 5 haiku run-routing rules + the haiku ModelAccount deleted.
- [x] The single redundant `is_default` rule deleted (the picker's `default_account_id`
      = sonnet is now the sole default).
- [x] Fixed the `plan → opus` rule: its target was an account **id** (which the
      resolver can't match → it silently fell back to sonnet); recreated with
      target `opus` (litellm_model) so plan work actually routes to opus.

## Deferred
- [ ] Manual visual pass in the running PWA after deploy.
