# telegram plugin — E2E checklist (T2)

Non-web component: the end-to-end surface is the plugin loaded by
`PluginLoader` and dispatched through `PluginRunner` exactly as the runtime
will. Each item is verified by an automated test (httpx mocked via respx —
**no real Telegram calls**, per the testing rules). Re-run:

```bash
uv run pytest tests/plugins/implementations/telegram -q
```

- [x] Plugin is discoverable by the loader (no central registry edit)
      — `test_plugin.py::TestLoaderDiscovery::test_loader_discovers_telegram`
- [x] Declares `data_jurisdiction="unknown"` + bot_token / webhook_secret
      credentials — `TestPluginMeta`
- [x] **Inbound**: valid secret-token message update → `TriggerEvent`
      (idempotency_key derived from Telegram `update_id`)
      — `TestInbound::test_inbound_parses_message`
- [x] **Inbound**: bad / missing secret token is rejected
      — `test_webhook.py::TestParseUpdate::test_bad_secret_token_raises`
      + `TestVerifySecretToken`
- [x] **Inbound**: idempotency key is stable per `update_id`
      — `test_webhook.py::TestParseUpdate::test_idempotency_key_from_update_id`
- [x] **Inbound**: non-message update / bot author → `None` (skip)
      — `test_webhook.py` skip cases
- [x] **Outbound** `telegram_message`: sends a message, returns `external_ref`
      + `compensation_handle` (tier `t2_trail`)
      — `TestOutbound::test_deliver_message_sends_and_returns_handle`
- [x] **Outbound**: Telegram `{"ok": false}` HTTP-200 error path raises (not
      treated as success) — `TestOutbound::test_deliver_message_ok_false_raises`
      + `test_client.py::TestSendMessage::test_send_message_ok_false_raises`
- [x] **Compensate** message: deletes the message; **idempotent** no-op when
      "message to delete not found" on re-call — `TestCompensate`
- [x] **Action** `send_message` (`mcp_exposed=True`) dispatch + input-schema
      validation — `TestActions`
- [x] **Setup**: ingests `TELEGRAM_BOT_TOKEN` / `TELEGRAM_WEBHOOK_SECRET` from
      the environment into the credential store — `TestSetup`

## Notes / deviations

- **data_jurisdiction = `unknown`**: Telegram is operated out of multiple
  regions, so neither `us` nor `eu` is accurate. The framework
  (`VALID_JURISDICTIONS` in `backend/plugins/base.py`) supports `unknown` for
  "unspecified/global", so that is used rather than the imprecise `us` the
  github/slack connectors default to.
- **Auth scheme**: Telegram does NOT sign the body (no HMAC / replay window
  like Slack). Its webhook auth is the shared **secret token** echoed in the
  `X-Telegram-Bot-Api-Secret-Token` header; we constant-time compare it.

## Not in this track (verified absent / deferred to the e2e wiring chunk)

- Wiring the inbound capability into an intake webhook route, and the
  outbound/compensate capabilities into the delivery dispatch routes — that is
  the Connector-inbound / delivery integration chunk.
- `telegram_message` is not yet a member of the canonical
  `backend.delivery.schema.ArtifactType` literal — the decorator accepts the
  string today; promoting it to the typed dispatcher is an integration-track
  change (same pattern as slack's `slack_message`).
