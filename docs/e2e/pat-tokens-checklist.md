# E2E — Personal Access Tokens (PAT)

**Why this exists.** The MCP OAuth flow lands its authorization code on
`http://localhost:<random-port>/callback`, which only works when the browser and
the MCP client sit on the same machine. Over a VS Code remote tunnel, an SSH
session, a headless server, or a launchd/cron job, there is no reachable
listener and the connection simply fails. Every remote MCP server we checked
(Sentry, Atlassian, Slack) has the same constraint; the industry escape hatch is
a static bearer token, which `api.bsvibe.dev` already advertises via
`bearer_methods_supported: ["header"]`.

A PAT is not a new credential type. It is an `oauth_access_tokens` row with no
expiry and a `pat:<name>` label, carried by the same ES256 JWT every other token
uses — so `/revoke`, `/introspect` and the `mcp:*` scopes work unchanged.

## PR1 — nullable expiry + DB as the expiry authority

- [x] `issue_access_token(expires_at=None)` omits the `exp` claim entirely (no far-future sentinel)
- [x] `verify_access_token` accepts a token with no `exp`, and still enforces `exp` when present
- [x] `oauth_access_tokens.expires_at` accepts NULL after `alembic upgrade head`
- [x] Downgrade restores NOT NULL on a fresh DB, and refuses when NULL-expiry rows exist
- [x] `/mcp` rejects a token whose row expired even though the JWT's `exp` is still in the future
- [x] `/mcp` accepts a never-expiring token (no `exp`, NULL `expires_at`)
- [x] Introspection reports `active=true` with `exp` omitted for a NULL-expiry row
- [x] `mypy backend/` and `ruff check` clean

## PR2 — issuance, listing, revocation

- [x] `POST /api/v1/oauth/pats` mints a PAT and returns the raw token EXACTLY once
- [x] The row lands with `label='pat:<name>'`, NULL `expires_at`, and the requested scopes
- [x] An explicit `expires_in_days` is honoured on both the row and the `exp` claim
- [x] No refresh token is created alongside a PAT (a PAT must not re-mint itself)
- [x] Scopes are validated against `ALLOWED_SCOPES`; an unknown one is a 400
- [x] Listing returns label / issued / expiry and never the token value
- [x] Listing excludes grant-issued tokens and revoked rows
- [x] Revoking sets `revoked_at` and drops the row from the listing; unknown id is a 404
- [x] Revocation is workspace-scoped — a bare id from another workspace is a 404
- [x] Minting derives `workspace_id` from the session; the body cannot name one

Deferred by design:

- **Last-used tracking** is not implemented. It needs a write on every `/mcp`
  request, which is a separate decision about hot-path cost.
- **No MCP tool for PAT management.** Everything the PWA can do is normally
  exposed over MCP too, but a `mcp:write` token that can mint more tokens is a
  privilege-escalation path — issuance stays PWA-only.
- **401 after revocation** is asserted end-to-end in PR4, not here; PR2 only
  proves the row flips.

## PR3 — PWA Settings UI

- [ ] Settings shows a PAT section: create, list, revoke
- [ ] The raw token is shown once with a copy affordance and an explicit "you will not see this again" warning
- [ ] Korean copy follows the 해요체 house style
- [ ] Revoke asks for confirmation and the row disappears on success

## PR4 — real-request E2E (no dependency overrides)

The half-wired failure mode this catches: issuance, schema and unit tests all
pass while nothing actually honours the token on the request path. Mocked tests
cannot see it.

- [ ] Mint a PAT through the real API against a real database
- [ ] Call `/mcp` with `Authorization: Bearer <pat>` — no `dependency_overrides`, no pre-seeded rows
- [ ] The MCP tool list comes back populated (not an empty list, not a 401)
- [ ] Revoke the PAT, repeat the call, and confirm 401 with the RFC 9728 `WWW-Authenticate` header
- [ ] From a machine with no browser: `claude mcp add --transport http bsvibe https://api.bsvibe.dev/mcp --header "Authorization: Bearer <pat>"` connects
