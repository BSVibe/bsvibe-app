# E2E Checklist — worker-managed Claude OAuth auto-refresh

Verified live on the mac-mini host executor after deploy.

- [ ] Seed `~/.bsvibe/claude_oauth.json` from the current credential; restart the
      host launchd worker (`launchctl kickstart -k`).
- [ ] A claude_code executor task authenticates (no `401 Failed to authenticate`)
      with the static plist `ANTHROPIC_AUTH_TOKEN` REMOVED — proving the worker
      injects its own refreshed token.
- [ ] After the access token's expiry window, a new task still authenticates
      (auto-refresh persisted a rotated pair to the credential file).
- [ ] No `~/.claude/.credentials.json` dependency: an interactive `claude /login`
      that rewrites that file does not break the worker (separate file/family).
