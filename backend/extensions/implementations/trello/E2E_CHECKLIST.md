# trello plugin — E2E checklist (T3)

Non-web component: the end-to-end surface is the plugin loaded by
`PluginLoader` and dispatched through `PluginRunner` exactly as the runtime
will. Each item is verified by an automated test (httpx mocked via respx —
**no real Trello calls**, per the testing rules). Re-run:

```bash
uv run pytest tests/plugins/implementations/trello -q
```

- [x] Plugin is discoverable by the loader (no central registry edit)
      — `test_plugin.py::TestLoaderDiscovery::test_loader_discovers_trello`
- [x] Declares `data_jurisdiction="us"` + `api_key` / `token` credentials
      — `TestPluginMeta`
- [x] **Outbound** `card`: creates a card, returns `external_ref`
      (`trello://card/<id>`) + `url` (`shortUrl`/`url`) +
      `compensation_handle` (tier `t3_new_artifact`)
      — `TestOutbound::test_deliver_card_*`
- [x] **Outbound**: `list_id` falls back to `config['trello_list_id']`,
      `desc` falls back to `event['body']` — `test_deliver_card_uses_list_from_config_and_body_fallback`
- [x] **Outbound**: missing credentials / missing list / API error → `PluginRunError`
      — `test_missing_credentials_raises` / `test_missing_list_raises` / `test_deliver_card_propagates_api_error`
- [x] **Compensate** card: archives the card (`PUT closed=true`);
      **idempotent** (404 → success on re-call) — `TestCompensate::test_archive_card_*`
- [x] **Action** `create_card` (`mcp_exposed=True`) dispatch + input-schema
      validation — `TestActions`
- [x] **Setup**: ingests `TRELLO_API_KEY` + `TRELLO_TOKEN` (+ optional
      `TRELLO_LIST_ID`) from the environment into the credential store
      — `TestSetup`
- [x] **Client**: httpx wrapper sends auth + payload as **query params**
      (`key` / `token` / `idList` / `name` / `desc`), NOT a Bearer header;
      non-2xx raises `TrelloApiError`; archive 404 returned not raised
      — `test_client.py`

## Auth scheme note

Trello does NOT use OAuth nor a `Bearer` Authorization header. The API key and
token ride on every request as query parameters (`?key=<api_key>&token=<token>`).
`TrelloClient._auth_params` appends them; the test asserts no `Authorization`
header is present.

## Compensation tier — `t3_new_artifact`

A created Trello card is a *new artifact*. The delivery flow has no clean
hard-delete; the best undo is to archive (`PUT /1/cards/{id}?closed=true`),
which leaves a closed card behind rather than removing it. This mirrors the
notion created-page and linear created-issue tiers (Workflow §9.1).

## Not in this track (deferred to the e2e wiring chunk)

- Wiring the outbound/compensate capabilities into the delivery dispatch
  routes (the dispatcher auto-routes by `artifact_type`, no edit needed here).
- Inbound webhooks (Trello card events) — delivery-only connector for now.
