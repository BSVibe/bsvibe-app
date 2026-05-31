# discord plugin — E2E checklist (T2)

Non-web component: the end-to-end surface is the plugin loaded by
`PluginLoader` and dispatched through `PluginRunner` exactly as the runtime
will. Each item is verified by an automated test (httpx mocked via respx —
**no real Discord calls**, per the testing rules; Ed25519 inbound tests
generate a throwaway keypair in-test). Re-run:

```bash
uv run pytest tests/plugins/implementations/discord -q
```

- [x] Plugin is discoverable by the loader (no central registry edit)
      — `test_plugin.py::TestLoaderDiscovery::test_loader_discovers_discord`
- [x] Declares `data_jurisdiction="us"` + bot_token / public_key credentials
      — `TestPluginMeta`
- [x] **Inbound**: valid Ed25519-signed command interaction → `TriggerEvent`
      (idempotency_key derived from interaction `id`)
      — `TestInbound::test_inbound_parses_command`
      + `test_webhook.py::TestParseInteraction::test_parses_command_into_trigger_event`
- [x] **Inbound**: bad Ed25519 signature is rejected (wrong key / tampered body)
      — `test_webhook.py::TestVerifySignature` + `TestParseInteraction::test_bad_signature_raises`
      + `TestInbound::test_inbound_bad_signature_rejected`
- [x] **Inbound**: PING (type=1) still verifies, then parse returns `None`
      (PONG is answered by the HTTP route — later chunk)
      — `TestInbound::test_inbound_ping_returns_none`
      + `test_webhook.py::TestParseInteraction::test_ping_returns_none_but_verifies`
      + `test_ping_with_bad_signature_still_rejected`
- [x] **Inbound**: idempotency key is stable per interaction `id`
      — `test_webhook.py::TestParseInteraction::test_idempotency_key_from_interaction_id`
- [x] **Inbound**: unsupported interaction type / bot author → `None` (skip)
      — `test_webhook.py` skip cases
- [x] **Outbound** `discord_message`: posts a message, returns `external_ref`
      + `compensation_handle` (tier `t2_trail`)
      — `TestOutbound::test_deliver_message_posts_and_returns_handle`
- [x] **Outbound**: Discord non-2xx error path raises (not treated as success)
      — `TestOutbound::test_deliver_message_api_error_raises`
      + `test_client.py::TestCreateMessage::test_create_message_non_2xx_raises`
- [x] **Compensate** message: deletes the message; **idempotent** no-op when
      `404 Not Found` on re-call — `TestCompensate`
- [x] **Action** `send_message` (`mcp_exposed=True`) dispatch + input-schema
      validation — `TestActions`
- [x] **Setup**: ingests `DISCORD_BOT_TOKEN` / `DISCORD_PUBLIC_KEY` from the
      environment into the credential store — `TestSetup`

## Notes / deviations

- **Auth scheme = Ed25519 request signing**. Discord signs every interaction
  delivery: `X-Signature-Ed25519` (hex signature) over the bytes
  `X-Signature-Timestamp + raw_body`, verified with the application's Ed25519
  public key. This is implemented with the **`cryptography`** library's
  `Ed25519PublicKey.verify` (the constant-time primitive) — `cryptography>=42`
  is **already a project dependency** (`pyproject.toml`), so **no new
  dependency** was added. (Unlike Slack's HMAC-SHA256-over-body or Telegram's
  shared secret-token header.) There is no timestamp/replay window step —
  Discord's scheme is signature-only; the timestamp is part of the signed
  message, not a separately validated freshness bound.
- **PING handling**: the registration PING (`type=1`) must still pass signature
  verification (a forged PING is rejected), but parsing returns `None` — the
  PONG response is sent by the HTTP route, which is out of this track's scope.
- **data_jurisdiction = `us`**: Discord Inc. is US-headquartered with a
  US-operated control plane, so `us` matches the github/slack connectors and is
  more accurate than `unknown` (which Telegram used because its operator is
  multi-region/unspecified).
- **API error convention** (differs from slack/telegram): Discord signals
  failure with a **non-2xx HTTP status** + JSON `{"message": ...}` body, not
  HTTP-200 `ok:false`. `DiscordClient` raises `DiscordApiError` on non-2xx, and
  treats `404` on delete as an idempotent no-op (message already gone).
- **No SDK**: external I/O goes through the thin `DiscordClient` httpx wrapper,
  matching slack/telegram (dependency surface stays at httpx).

## Not in this track (verified absent / deferred to the e2e wiring chunk)

- Wiring the inbound capability into an intake webhook route (including
  answering the PING handshake with a PONG over HTTP), and the
  outbound/compensate capabilities into the delivery dispatch routes — that is
  the Connector-inbound / delivery integration chunk.
- `discord_message` is not yet a member of the canonical
  `backend.delivery.schema.ArtifactType` literal — the decorator accepts the
  string today; promoting it to the typed dispatcher is an integration-track
  change (same pattern as slack's `slack_message` / telegram's
  `telegram_message`).
