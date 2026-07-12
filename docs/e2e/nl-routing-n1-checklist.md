# E2E Checklist — NL-native Routing Lift N1 (intent classifier)

Re-introduce the embedding intent classifier (deleted in Lift 2) and wire it into
run-routing so rules can key on `classified_intent` (semantic category routing).
Foundation only — user-visible once intents (N2) + category rules (N3) exist.
Embeddings/DB mocked in tests (no real embedding API).

## Classifier (`tests/router/routing/run_routing/test_intent_classifier.py`)
- [x] Classifies text to the nearest intent above threshold (stub embedder +
      in-memory vector backend).
- [x] Picks the higher-similarity intent between two.
- [x] Below every threshold → `None` (never a wrong category).
- [x] Empty text / no intents → `None`.
- [x] Embedder failure → `None` (a classify hiccup never breaks routing).
- [x] `ServiceAsEmbedder` adapts an `embed_one`-shaped coroutine.

## Resolver wiring (`tests/dispatch/test_resolver.py`)
- [x] A rule conditioned on `classified_intent == marketing` matches when the
      (injected) classifier returns "marketing" → routes to that rule's account.
- [x] Classifier miss (different intent) → the condition fails → no match →
      `NoMatchingRouteError` (no silent pick).
- [x] `_needs_classified_intent` gate: true only when a rule keys on
      `classified_intent` — the resolver skips the classifier (and its reads)
      otherwise.

## Factory / prod wiring
- [x] `build_intent_classifier` returns `None` (clean no-op) when the account has
      no embedding config — `test_build_classifier_is_none_without_embedding_config`.
- [x] Wired lazily at `_resolve_via_caller`: the builder (personal-account +
      embedding-config + intents reads) fires ONLY when a rule needs
      classified_intent, so the common path pays nothing.
- [x] No regression: dispatch / router / workflow / import-contracts (284 passed).

## Deferred (next lifts)
- [ ] N2: define intents (categories) — `POST /api/v1/intents` + NL-derived.
- [ ] N3: NL compiler emits `classified_intent` + complexity/language condition
      rules (multi-dimension), not just caller rules.
- [ ] N4: PWA NL-first surface.
