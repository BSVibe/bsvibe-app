# E2E — `bsvibe_connectors_set_delivery_config` (MCP/PWA parity)

Closes the MCP-UI parity gap: the PWA (via `PATCH /connectors/{id}`) could set a
connector's `delivery_config`, but MCP had no equivalent — so pointing GitHub PR
delivery + auto-merge at a repo required the PWA or a raw DB write. This tool is
the MCP equivalent, mirroring the REST PATCH shallow-merge + secret redaction.

## Behavior (unit-verified)
- [x] Shallow-merges the partial `delivery_config` into the stored config (unspecified keys preserved), reassigning a fresh dict so the JSON column flushes.
- [x] Workspace-scoped: a connector in another workspace → `connector not found` (ToolError).
- [x] `mcp:write` required — a read-only principal is rejected (`requires scope`).
- [x] Response redacts secret keys (`webhook_secret`/`signing_secret`/`client_secret`); the stored row keeps them (the ingress still needs them). NOTE: this also fixed the pre-existing leak where `list`/`show` echoed these unredacted.
- [x] `extra="forbid"` on the input (parity with REST `ConnectorUpdate`).

## Live E2E (prod MCP, dogfood workspace)
- [ ] `bsvibe_connectors_list` → find the GitHub connector id.
- [ ] `bsvibe_connectors_set_delivery_config {connector_id, delivery_config:{repo:"BSVibe/bsvibe-app"}}` → response shows `delivery_config.repo` set, no secret keys.
- [ ] `bsvibe_connectors_show {connector_id}` → `repo` present; any `webhook_secret` NOT present in the response.
- [ ] Confirm `resolve_github_binding` now resolves this repo: a delivered GitHub deliverable opens a PR against `BSVibe/bsvibe-app` (the auto-merge target the delivery_config drives), i.e. the value set via MCP is actually consumed by the PR-delivery path.
- [ ] Setting a partial update (e.g. only `{base_branch:"main"}`) preserves the previously-set `repo` (shallow merge, live).

## Parity confirmation
- [ ] Same effect as the PWA connector settings / `PATCH /connectors/{id}` — verify a value set via MCP is visible in the PWA connector view and vice-versa (one `connector_accounts.delivery_config` row, two write surfaces).
