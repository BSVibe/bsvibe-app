# A workspace's model account cannot aim the server at its own network

`ModelAccount.api_base` and `extra_params` (merged last into `litellm.acompletion`'s kwargs)
were never checked. With public signup, any workspace could point them at `postgres:5432`,
`169.254.169.254`, or the host's Ollama (`host.docker.internal:11434` — the embedding model
lives there) and have the backend make that request on every turn.

Now:
- `LlmClient.chat` checks the MERGED kwargs before every call: every endpoint must be http(s)
  and resolve entirely to global addresses; an `ollama/` · `ollama_chat/` · `hosted_vllm/` …
  model must name one (LiteLLM's default is the server's loopback)
- saving an account (REST create/patch, MCP create) runs the same check → 422 / ToolError
- executor accounts are exempt (they run on the founder's worker, not the server)
- `BSVIBE_LLM_EGRESS_ALLOWED_HOSTS` — deployment-wide exact-host exemptions, empty by default

Backend only — autodeployed. No migration.

## Unit / wire (local)

- [x] 9 inward endpoints refused before any request (loopback, RFC1918, link-local, CGNAT, `::1`, `0.0.0.0`)
- [x] a public name resolving to ANY private address is refused; docker service names refused
- [x] `extra_params.base_url` and `extra_params.api_base` cannot route around the check
- [x] local-runtime models without an endpoint refused; `file://` refused; unresolvable refused
- [x] controls: public endpoint, hosted provider with no endpoint, operator allowlist
- [x] REST create/patch → 422 with nothing saved/changed; MCP create → ToolError; executor exempt
- [x] wire cuts (10: call site · `base_url` key · all-addresses · loopback default · allowlist · create · patch · executor exemption · REST map · MCP map) each red

## Prod (after deploy)

- [x] `bsvibe_model_accounts_create` with `api_base=http://169.254.169.254` → refused, nothing listed (measured 2026-10-15)
- [ ] Existing accounts in OTHER workspaces that point inward will now fail their runs with
      `run_drive_failed` carrying "is not a public address". 형님 DB read to know beforehand:
      `SELECT workspace_id, provider, litellm_model, api_base FROM model_accounts WHERE provider <> 'executor' AND (api_base IS NULL AND litellm_model ~ '^(ollama|ollama_chat|lm_studio|hosted_vllm|vllm|llamafile)/' OR api_base ~* '(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|host\.docker\.internal)')`

## Not closed

- DNS rebinding: the name is resolved here and again by the HTTP client. An egress firewall on
  the backend container is the complete answer.
