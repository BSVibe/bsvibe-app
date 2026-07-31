# E2E — `bsvibe-worker claude-login` (independent Claude OAuth family)

Goal: the host worker mints its OWN Claude OAuth token (fresh authorize grant)
into `~/.bsvibe/claude_oauth.json`, so it no longer shares a refresh family with
the interactive `claude` CLI login — eliminating the mutual-burn (shared family →
CLI's single-use rotation invalidates the worker's copy).

Measured facts this builds on (2026-07-31):
- Minting an independent token via a fresh authorize flow did NOT burn the CLI
  login (CLI creds byte-identical + `claude auth status` intact afterward) →
  independent token grants coexist on one account.
- The interactive CLI login's scope (`org:create_api_key user:profile
  user:inference user:sessions:claude_code user:mcp_servers user:file_upload`) is
  what yields a REFRESHING token; `setup-token`'s `user:inference` alone does not.

## Preconditions
- [ ] Host has the worker installed (editable venv at `~/Works/bsvibe-app/main/.venv`).
- [ ] Snapshot the CLI creds BEFORE, to prove non-destructiveness:
      `python3 -c "import json;d=json.load(open('~/.claude/.credentials.json'.replace('~',__import__('os').path.expanduser('~')))['claudeAiOauth'];print(d['refreshToken'][:24])"`

## Manual (remote/headless) flow — the founder's common path
- [ ] Run `bsvibe-worker claude-login --manual`. It prints an authorize URL.
- [ ] Open the URL in a browser, approve. Claude shows `code#state` (or a failing
      `127.0.0.1/...` redirect whose URL bar carries `?code=&state=`).
- [ ] Paste the `code#state` (or full redirect URL) back at the prompt.
- [ ] Command prints `Claude token saved to ~/.bsvibe/claude_oauth.json (expires_at=…)`.

## Verify the minted token
- [ ] `~/.bsvibe/claude_oauth.json` exists, mode `0600`, flat shape
      `{access_token, refresh_token, expires_at}` with a `sk-ant-ort01-…` refresh
      token and a future `expires_at` (ms).
- [ ] Restart the host worker (`bsvibe-worker run`). Logs show framing/drive
      dispatch succeeds using the injected `ANTHROPIC_AUTH_TOKEN` (a live run
      completes end-to-end).

## Verify independence (the whole point)
- [ ] CLI creds UNCHANGED vs the BEFORE snapshot (same `refreshToken` prefix);
      `claude auth status` still `loggedIn: true`, `subscriptionType: max`.
- [ ] Over ~10–15 min the keepalive (#641) logs `claude_auth_keepalive_ok` and,
      when within the 600s buffer, `claude_oauth_refreshed` — the worker's OWN
      family rotates in `~/.bsvibe/claude_oauth.json` while the CLI creds stay put.
- [ ] `claude_oauth_cli_fallback_used` count stays 0 (worker never needs to borrow
      the CLI access token — it has a healthy independent family).

## Durability soak (the real proof — unit green ≠ prod durable)
- [ ] Leave the worker running >32h (past the window where the OLD shared-family
      seed mutual-burned). Expect NO `refresh_invalid_grant`, NO outage; the
      worker's token self-refreshes independently the whole time.

## Loopback flow (local console, optional)
- [ ] On the Mac Mini console, `bsvibe-worker claude-login` (no `--manual`) opens
      the browser and captures the callback on `127.0.0.1:<port>/callback`
      automatically; same persisted result.
