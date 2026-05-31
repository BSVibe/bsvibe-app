# email-sender plugin — E2E checklist (T4, outbound-only)

Non-web component: the end-to-end surface is the plugin loaded by
`PluginLoader` and dispatched through `PluginRunner` exactly as the runtime
will. Each item is verified by an automated test (httpx mocked via respx —
**no real Resend calls**, per the testing rules). Re-run:

```bash
uv run pytest tests/plugins/implementations/email_sender -q
```

- [x] Plugin is discoverable by the loader (no central registry edit)
      — `test_plugin.py::TestLoaderDiscovery::test_loader_discovers_email_sender`
- [x] Declares `data_jurisdiction="us"` (Resend is US) + `api_key` / `from`
      credentials — `TestPluginMeta`
- [x] **Outbound-only**: no `@p.inbound` capability
      — `TestPluginMeta::test_no_inbound` / loader discovery `inbounds == []`
- [x] **Outbound** `email`: sends via Resend, returns `external_ref` +
      `compensation_handle` (tier `t4_irreversible`, `compensation_supported=False`)
      — `TestOutbound::test_deliver_email_*`
- [x] **Outbound**: `as_text` sends a text body; `from` resolves from
      event > config > credential — `TestOutbound::test_deliver_email_as_text` /
      `test_deliver_email_uses_from_from_config`
- [x] **Outbound**: missing api_key / missing from → `PluginRunError`; a Resend
      non-2xx surfaces as `PluginRunError`
      — `TestOutbound::test_missing_*` / `test_outbound_http_error_*`
- [x] **Compensate** email: records uncompensable (T4 — a sent email cannot be
      recalled), **idempotent**, and makes **no remote call**
      — `TestCompensate`
- [x] **Action** `send_email` (`mcp_exposed=True`) dispatch + input-schema
      validation — `TestActions`
- [x] **Setup**: ingests `RESEND_API_KEY` (+ optional `RESEND_FROM`) from the
      environment into the credential store — `TestSetup`
- [x] **Client**: httpx wrapper sends `Authorization: Bearer` + JSON
      `{from,to,subject,html|text}`; non-2xx raises `EmailApiError` with
      status + parsed message (HTTP status, not a 200+ok:false quirk)
      — `test_client.py`

## Compensation tier — T4 irreversible (justification)

A transactional email, once accepted by Resend, **cannot be recalled or unsent**
through any provider API. The plugin declares `compensation_tier="t4_irreversible"`
with `compensation_supported=False`. The paired `@p.compensate` handler is a
notify-style no-op: it records that no clean undo exists (idempotent, no remote
call) rather than pretending to revert. A retraction/correction would be a *new*
email the agent authors explicitly — modelling that as an automatic `t3_new_artifact`
undo would mislead the dispatcher, so it is deliberately not used here.

## Not in this track (deferred to the e2e wiring chunk)

- Wiring the outbound/compensate capabilities into the delivery dispatch
  routes (the dispatcher auto-routes by `artifact_type`, no edit needed here).
- Attachment support and per-recipient batching — today the outbound sends a
  single email with one `body` (html or text).
