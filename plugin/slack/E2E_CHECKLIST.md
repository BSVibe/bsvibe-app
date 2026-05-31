# slack plugin — E2E checklist (T2)

Non-web component: the end-to-end surface is the plugin loaded by
`PluginLoader` and dispatched through `PluginRunner` exactly as the runtime
will. Each item is verified by an automated test (httpx mocked via respx —
**no real Slack calls**, per the testing rules). Re-run:

```bash
uv run pytest tests/plugins/implementations/slack -q
```

- [x] Plugin is discoverable by the loader (no central registry edit)
      — `test_plugin.py::TestLoaderDiscovery::test_loader_discovers_slack`
- [x] Declares `data_jurisdiction="us"` + bot_token / signing_secret credentials
      — `TestPluginMeta`
- [x] **Inbound**: valid signed app_mention event → `TriggerEvent`
      (idempotency_key = Slack `event_id`)
      — `TestInbound::test_inbound_parses_app_mention`
- [x] **Inbound**: bad signing-secret HMAC is rejected
      — `test_webhook.py::TestParseEvent::test_bad_signature_raises`
- [x] **Inbound**: stale timestamp (> 5 min) is rejected (replay guard)
      — `test_webhook.py::TestParseEvent::test_stale_timestamp_raises`
      + `TestVerifySignature::test_rejects_stale_timestamp`
- [x] **Inbound**: url_verification handshake / unsupported event / bot
      author → `None` (skip)
      — `test_webhook.py` skip cases
- [x] **Outbound** `slack_message`: posts a message, returns `external_ref`
      + `compensation_handle` (tier `t2_trail`)
      — `TestOutbound::test_deliver_message_posts_and_returns_handle`
- [x] **Outbound**: Slack `{"ok": false}` HTTP-200 error path raises (not
      treated as success) — `TestOutbound::test_deliver_message_ok_false_raises`
      + `test_client.py::TestPostMessage::test_post_message_ok_false_raises`
- [x] **Compensate** message: deletes the message; **idempotent** no-op when
      `message_not_found` on re-call — `TestCompensate`
- [x] **Action** `post_message` (`mcp_exposed=True`) dispatch + input-schema
      validation — `TestActions`
- [x] **Setup**: ingests `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` from the
      environment into the credential store — `TestSetup`

## Not in this track (verified absent / deferred to the e2e wiring chunk)

- Wiring the inbound capability into an intake webhook route (including
  answering the `url_verification` challenge over HTTP), and the
  outbound/compensate capabilities into the delivery dispatch routes —
  that is the Connector-inbound / delivery integration chunk.
- `slack_message` is not yet a member of the canonical
  `backend.delivery.schema.ArtifactType` literal — the decorator accepts the
  string today; promoting it to the typed dispatcher is an integration-track
  change (same pattern as github's `issue_comment`).
