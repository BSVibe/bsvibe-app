# E2E Checklist — Unified Routing Lift 1 (content-signal absorption)

Non-web, backend-internal change. Verified via the run-routing engine unit suite
(`tests/router/routing/run_routing/test_engine.py`) + the MCP tool suite
(`tests/mcp/test_run_routing_rules_tools.py`) exercising the real
`RoutingContext.from_run`, `evaluate_rules`, and the create-rule validation path.

Scope: absorb the worth-keeping Layer-2 content signals
(`estimated_tokens` / `classified_intent` / `detected_language`) into the single
run-routing rule surface. **Additive only** — no existing rule changes behavior.

- [x] The three signals are addressable condition fields (`ALLOWED_FIELDS`) —
      `test_content_signal_fields_are_addressable`.
- [x] A `RoutingContext` built without the signals defaults to the "no signal"
      values (`estimated_tokens=0`, others `None`) —
      `test_content_signals_default_to_empty_when_absent`.
- [x] `RoutingContext.from_run` estimates tokens from the run's intent text, and
      is zero (never crashes) on empty payload —
      `test_from_run_estimates_tokens_from_intent_text`.
- [x] `from_run` detects language (ko/en) from intent text, `None` when absent —
      `test_from_run_detects_language_from_intent_text`.
- [x] `from_run` reads `classified_intent` from the frame, `None` when missing/
      non-str — `test_from_run_reads_classified_intent_from_frame`.
- [x] A rule can match on a content signal (`estimated_tokens gt`,
      `detected_language eq`) and correctly not-match otherwise —
      `test_rule_can_match_on_content_signal`.
- [x] Additive guarantee: a rule authored against the old field set matches
      identically regardless of the new signals' values —
      `test_content_signals_do_not_change_existing_rule_matching`.
- [x] Rule-authoring validation accepts the absorbed fields end-to-end via MCP
      create — `test_create_accepts_absorbed_content_signal_field`; a
      non-absorbed legacy field (`user_text`) is still rejected —
      `test_create_rejects_unknown_field_in_conditions`.
- [x] No regression across dispatch / router / litellm-hook suites (316 passed).

## Deferred to later lifts (out of Lift 1 scope)
- [ ] External `/chat/completions` builds a `RoutingContext` from request content
      and routes through `ModelAccountResolver` (Lift 2).
- [ ] Layer-2 model-routing engine + tables hard-deleted (Lift 3).
