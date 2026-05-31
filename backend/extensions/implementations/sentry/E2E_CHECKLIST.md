# sentry plugin — E2E checklist (T2)

Non-web component: the end-to-end surface is the plugin loaded by
`PluginLoader` and dispatched through `PluginRunner` exactly as the runtime
will. Each item is verified by an automated test (httpx mocked via respx —
**no real Sentry calls**, per the testing rules). Re-run:

```bash
uv run pytest tests/plugins/implementations/sentry -q
```

- [x] Plugin is discoverable by the loader (no central registry edit)
      — `test_plugin.py::TestLoaderDiscovery::test_loader_discovers_sentry`
- [x] Declares `data_jurisdiction="us"` + auth_token / client_secret credentials
      — `TestPluginMeta`
- [x] **Inbound**: valid signed `issue` webhook → `TriggerEvent`
      (idempotency_key derived from the Sentry hook/issue id)
      — `TestInbound::test_inbound_parses_issue_webhook`
- [x] **Inbound**: HMAC-SHA256 over the raw body, bare hex digest in the
      `Sentry-Hook-Signature` header, constant-time compare; bad signature
      rejected — `test_webhook.py::TestVerifySignature` + `TestParseWebhook::test_bad_signature_raises`
- [x] **Inbound**: unsupported `Sentry-Hook-Resource` (e.g. `installation`)
      → `None` (skip); `event_alert` is supported
      — `test_webhook.py` resource cases
- [x] **Inbound**: stable idempotency key (same hook id → same key)
      — `test_webhook.py::TestParseWebhook::test_idempotency_key_stable`
- [x] **Outbound** `sentry_issue_update`: resolves an issue, returns
      `external_ref` + `url` + `compensation_handle` (tier `t2_trail`)
      — `TestOutbound::test_deliver_resolve_returns_handle`
- [x] **Outbound**: a non-2xx Sentry response → `PluginRunError`
      — `TestOutbound::test_deliver_resolve_error_path`
- [x] **Compensate**: re-opens the issue (`status:unresolved`); **idempotent**
      no-op (404 → success) — `TestCompensate::test_revert_resolve_*`
- [x] **Action** `resolve_issue` (`mcp_exposed=True`) dispatch +
      input-schema validation — `TestActions`
- [x] **Setup**: ingests `SENTRY_AUTH_TOKEN` / `SENTRY_CLIENT_SECRET` from the
      environment into the credential store — `TestSetup`

## Not in this track (verified absent / deferred to the e2e wiring chunk)

- Wiring the inbound parser into the connector ingress import-map
  (`backend/connectors/resolver.py::_PARSERS` + `backend/api/webhooks.py`
  signature-error union), and the outbound/compensate capabilities into the
  delivery dispatch routes.
- `sentry_issue_update` is not yet a member of the canonical
  `backend.delivery.schema.ArtifactType` literal — the decorator accepts the
  string today; promoting it to the typed dispatcher is an integration-track
  change.
