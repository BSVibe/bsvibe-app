# E2E Checklist — Unified Routing Lift 3 (external chat gateway → resolver)

Backend change. The external `/api/v1/chat/completions` gateway now routes its
ModelAccount through the SAME `ModelAccountResolver` the internal callers use
(caller `chat.completions`), instead of demanding an explicit
`metadata.bsvibe_model_account_id`. Verified via `tests/api/test_chat_routing.py`
exercising the real `_resolve_chat_model_account_id` + resolver against an
in-memory session, plus the caller-registry + import-contract suites.

- [x] `chat.completions` is a registered caller (`KNOWN_CALLERS`, requires `chat`) —
      `test_chat_completions_is_a_known_caller`.
- [x] An explicit `metadata.bsvibe_model_account_id` still wins (override, resolver
      not consulted) — `test_explicit_account_id_is_used_verbatim`.
- [x] No explicit id + a workspace default → routes to the default account —
      `test_falls_back_to_workspace_default_when_no_explicit`.
- [x] No explicit id + a matching run-routing rule → the rule beats the workspace
      default — `test_matching_rule_beats_workspace_default`.
- [x] No explicit id + no rule + no default → raises `NoMatchingRouteError`, which
      the endpoint surfaces as a 400 (never a silent pick) —
      `test_no_route_no_default_raises`.
- [x] `chat.py` → `dispatch.resolver` import is allowed by the import-linter
      (contracts suite green); no double-swap possible (Layer 2 deleted in Lift 2).
- [x] No regression across dispatch / api / mcp suites (699 passed).

## Deferred
- [ ] Content-signal conditions for chat (estimated_tokens / detected_language on
      the request body) — the resolver builds a caller-only context for chat today;
      a `from_request` context is a later refinement (Lift 5-adjacent).
