# notion plugin — E2E checklist (T3)

Non-web component: the end-to-end surface is the plugin loaded by
`PluginLoader` and dispatched through `PluginRunner` exactly as the runtime
will. Each item is verified by an automated test (httpx mocked via respx —
**no real Notion calls**, per the testing rules). Re-run:

```bash
uv run pytest tests/plugins/implementations/notion -q
```

- [x] Plugin is discoverable by the loader (no central registry edit)
      — `test_plugin.py::TestLoaderDiscovery::test_loader_discovers_notion`
- [x] Declares `data_jurisdiction="us"` + `token` credential
      — `TestPluginMeta`
- [x] **Outbound** `page` / `page_image`: creates a page, returns
      `external_ref` + `url` + `compensation_handle` (tier `t3_new_artifact`)
      — `TestOutbound::test_deliver_page_*`
- [x] **Outbound**: missing token / missing parent → `PluginRunError`
      — `TestOutbound::test_missing_token_raises` / `test_missing_parent_raises`
- [x] **Compensate** page: archives the page; **idempotent** (404 → success on
      re-call) — `TestCompensate::test_archive_page_*`
- [x] **Actions** `create_page` / `append` (`mcp_exposed=True`) dispatch +
      input-schema validation — `TestActions`
- [x] **Setup**: ingests `NOTION_TOKEN` from the environment into the
      credential store — `TestSetup`
- [x] **Client**: httpx wrapper sends `Authorization: Bearer` + `Notion-Version`
      headers; HTTP error path raises `HTTPStatusError`; archive 404 returned
      not raised — `test_client.py`

## Not in this track (deferred to the e2e wiring chunk)

- Wiring the outbound/compensate capabilities into the delivery dispatch
  routes (the dispatcher auto-routes by `artifact_type`, no edit needed here).
- `page_image` rendering of an image deliverable into a real Notion image
  block — today the outbound treats it the same as a text page.
