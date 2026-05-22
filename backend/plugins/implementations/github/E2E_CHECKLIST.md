# github plugin — E2E checklist (T2)

Non-web component: the end-to-end surface is the plugin loaded by
`PluginLoader` and dispatched through `PluginRunner` exactly as the runtime
will. Each item is verified by an automated test (httpx mocked via respx —
**no real GitHub calls**, per the testing rules). Re-run:

```bash
uv run pytest tests/plugins/implementations/github -q
```

- [x] Plugin is discoverable by the loader (no central registry edit)
      — `test_plugin.py::TestLoaderDiscovery::test_loader_discovers_github`
- [x] Declares `data_jurisdiction="us"` + token / webhook_secret credentials
      — `TestPluginMeta`
- [x] **Inbound**: valid signed PR webhook → `TriggerEvent`
      (idempotency_key = `X-GitHub-Delivery`)
      — `TestInbound::test_inbound_parses_pr_webhook`
- [x] **Inbound**: bad HMAC signature is rejected
      — `test_webhook.py::TestParseWebhook::test_bad_signature_raises`
- [x] **Inbound**: ping / unsupported event / uninteresting action / bot
      sender → `None` (skip)
      — `test_webhook.py` skip cases
- [x] **Outbound** `pr` / `code`: opens (or updates) a PR, returns
      `external_ref` + `url` + `compensation_handle` (tier `t2_trail`)
      — `TestOutbound::test_deliver_pr_*`
- [x] **Outbound** `issue_comment`: posts a comment, returns handle
      (tier `t1_clean`) — `TestOutbound::test_deliver_comment_returns_handle`
- [x] **Compensate** PR: closes an open PR; **idempotent** no-op when already
      closed — `TestCompensate::test_close_pr_*`
- [x] **Compensate** comment: deletes; **idempotent** (404 → success on
      re-call) — `TestCompensate::test_delete_comment_idempotent`
- [x] **Actions** `open_pr` / `comment` (`mcp_exposed=True`) dispatch +
      input-schema validation — `TestActions`
- [x] **Setup**: ingests `GITHUB_TOKEN` / `GITHUB_WEBHOOK_SECRET` from the
      environment into the credential store — `TestSetup`

## Not in this track (verified absent / deferred to the e2e wiring chunk)

- Wiring the inbound capability into an intake webhook route, and the
  outbound/compensate capabilities into the delivery dispatch routes.
- `issue_comment` is not yet a member of the canonical
  `backend.delivery.schema.ArtifactType` literal — the decorator accepts the
  string today; promoting it to the typed dispatcher is an integration-track
  change.
