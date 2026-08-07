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

- [ ] `POST` mints a PAT, returns the raw token EXACTLY once, and never again
- [ ] The row lands with `label='pat:<name>'`, NULL `expires_at`, and the requested scopes
- [ ] No refresh token is created alongside a PAT (a PAT must not re-mint itself)
- [ ] Requested scopes cannot exceed the caller's own scopes
- [ ] Listing shows label / created / last-used and never the token value
- [ ] Revoking a PAT sets `revoked_at`; the next `/mcp` call with it returns 401
- [ ] The PAT is bound to the caller's workspace; it cannot be minted for another one

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
