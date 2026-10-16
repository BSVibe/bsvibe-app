# E2E — a server_sandbox product's commands get its declared secrets (#691)

Scope: `products.metadata.verify_secrets` → `declared_secrets_for_product` → the run's box
(`with_declared_env` on the loop path, `with_session_env` on the MCP work-tool path) →
`docker exec -e NAME` with the value only in the docker CLI's environment.

Before: `DockerSandboxSession.exec` dropped `env`, and a non-client_attach run got the shared
box with nothing on it. A product needing its own API key could be written but not run on the
server. client_attach already carried them (see `product-verify-secrets-checklist.md`).

## Live checks

- [ ] **The value is visible inside the box** ⭐
      On a `server_sandbox` product, declare `{"verify_secrets":{"BSVIBE_T691":"<value>"}}` via
      `bsvibe_products_set_metadata`. Run a task whose agent runs
      `sh -c 'test -n "$BSVIBE_T691" && echo present'` through `shell_exec` → `present`.
- [ ] **No value on any argv or log** ⭐
      While that run is active: `docker --context colima exec bsvibe-prod-dind-1 ps -ef` (or the
      host process list) shows `docker exec -e BSVIBE_T691 ...` — the name, never the value.
      `docker --context colima logs bsvibe-prod-worker-1 | grep <value>` → 0 lines.
- [ ] **A product declaring nothing is unchanged**
      Another server_sandbox product's run behaves as before (no `-e` flags on its execs).
- [ ] Remove the test secret afterwards (`verify_secrets: {"BSVIBE_T691": ""}` deletes it).

## Automated cover

- `tests/workflow/test_server_sandbox_carries_declared_secrets.py` — `-e NAME` only on argv,
  value in the subprocess env, caller env wins, wrapper keeps `runs_in_place`/`provisions_venv`,
  host fallback honours env, loop seam, MCP seam, composition-root injection. Every production
  edge was cut once and turned a test red.

## Known limits

- Every command in the box can read the values — the agent's included. That is the founder
  decision of 2026-08-24 ("manage env centrally on the server"), the same exposure client_attach
  runs already have.
