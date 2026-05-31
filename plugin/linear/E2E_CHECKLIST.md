# linear plugin — E2E checklist (T3)

Non-web component: the end-to-end surface is the plugin loaded by
`PluginLoader` and dispatched through `PluginRunner` exactly as the runtime
will. Each item is verified by an automated test (httpx mocked via respx —
**no real Linear calls**, per the testing rules). Re-run:

```bash
uv run pytest tests/plugins/implementations/linear -q
```

- [x] Plugin is discoverable by the loader (no central registry edit)
      — `test_plugin.py::TestLoaderDiscovery::test_loader_discovers_linear`
- [x] Declares `data_jurisdiction="us"` + `api_key` credential
      — `TestPluginMeta`
- [x] **Outbound** `issue`: creates a Linear issue via GraphQL `issueCreate`,
      returns `external_ref` (`linear://issue/<id>`) + `url` +
      `compensation_handle` (tier `t3_new_artifact`)
      — `TestOutbound::test_deliver_issue_*`
- [x] **Outbound**: team_id falls back to `config['linear_team_id']`;
      `description` falls back to `body` — `test_deliver_issue_uses_team_from_config_and_body_fallback`
- [x] **Outbound**: missing api_key / missing team_id → `PluginRunError`;
      a GraphQL `errors` envelope propagates as `PluginRunError`
      — `test_missing_api_key_raises` / `test_missing_team_raises` /
      `test_deliver_issue_propagates_graphql_error`
- [x] **Compensate** issue: archives the issue via GraphQL `issueArchive`;
      **idempotent** (`entityNotFound` → success on re-call); a different
      GraphQL error re-raises — `TestCompensate`
- [x] **Action** `create_issue` (`mcp_exposed=True`) dispatch + input-schema
      validation — `TestActions`
- [x] **Setup**: ingests `LINEAR_API_KEY` (+ optional `LINEAR_TEAM_ID`) from the
      environment into the credential store — `TestSetup`
- [x] **Client**: httpx GraphQL wrapper sends the api key **raw** in
      `Authorization` (NOT `Bearer`); a 200 carrying `{"errors": [...]}` raises
      `LinearApiError` (200 ≠ success); non-2xx raises `HTTPStatusError`
      — `test_client.py`

## GraphQL `errors` handling

The Linear GraphQL endpoint can return HTTP 200 with `{"errors": [...]}`
(query/mutation-level errors). `LinearClient._data` therefore inspects the body
*after* `raise_for_status()` and raises `LinearApiError` when `errors` is
present — a 200 is never treated as success on faith. The error's `extensions.code`
(`entityNotFound`) is carried on the raised `LinearApiError`, which the
compensate handler uses to make archive idempotent.

## Compensation tier justification (`t3_new_artifact`)

A created Linear issue is a *new artifact*: the public API has no clean
hard-delete in place, only archive/cancel. The best undo is therefore to
archive the issue, mirroring notion's created-page tier. This is `t3` (the
artifact still exists, archived) rather than `t1`/`t2`.

## Not in this track (deferred to later chunks)

- `@p.inbound` — Linear inbound webhooks (issue/comment events).
- Wiring the outbound/compensate capabilities into the delivery dispatch
  routes (the dispatcher auto-routes by `artifact_type`, no edit needed here).
