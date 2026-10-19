# E2E — codex acts through BSVibe's tools only (#1000)

Founder decision 2026-10-19: codex gets the same treatment as claude_code and opencode. An agent run acts only through BSVibe's MCP work tools, with codex's own tools stripped.

## Measured on codex-cli 0.163.0 (2026-10-19)

All rows below were read off the request body codex sends its model provider. A local capture server stood in for the provider, so no model tokens were spent.

| Setting | Built-ins the model gets |
|---|---|
| gpt-5.5, no flags | 10 — `exec_command` `write_stdin` `apply_patch` `web_search` `tool_search` `view_image` `request_user_input` + 3 goal tools |
| gpt-5.1-codex, our flags | **`request_user_input` only** (no file, no command) |
| gpt-5.5, all 118 flags off | `apply_patch` **survives** (a native write tool) → agent runs are pinned to models measured without it: gpt-5.1-codex · gpt-5.1 · o3 |

**Flags:** `--disable` shell_tool · multi_agent · goals · view_image · browser_use · computer_use · in_app_browser · image_generation, plus `-c web_search="disabled"` and `--sandbox read-only`. `view_image` became a feature flag after 0.130, which removes the reason codex was refused on 2026-09-19.

**MCP:** `CODEX_HOME/config.toml` contains `[mcp_servers.bsvibe] url · bearer_token_env_var · enabled_tools`.
- The server received `Authorization: Bearer <token>` from the env var. The token never appears on argv or in a file.
- `enabled_tools` dropped a tool the server advertised that was not on the list.
- 🚨 A misspelt `enabled_toolz` is ignored silently, and then every advertised tool gets through. `test_the_mcp_server_is_ours_with_exactly_the_dispatched_tools` pins the exact key.
- Besides our tools, codex adds `list_mcp_resources`, `list_mcp_resource_templates` and `read_mcp_resource`. These only reach *our* server.

**Home:** each run gets its own `CODEX_HOME` holding a copy of `auth.json` (0600). `codex login status` reports "Logged in using ChatGPT" there; an empty home reports "Not logged in". The host `~/.codex` (personal MCP servers, memories DB) is never used. The run's home is removed when the run ends.

**Real binary × real `CodexExecutor`** (provider = capture server):
- model `gpt-5.1-codex`;
- tools = `mcp__bsvibe{bsvibe_work_file_read, bsvibe_work_shell_exec}` + `request_user_input` + the 3 resource readers;
- bearer arrived;
- 0 leftover homes.

## Live checks (on the worker host)

- [ ] `npm i -g @openai/codex` is on the worker's PATH, and `codex login` was done as the worker's user, or `BSVIBE_WORKER_CODEX_AUTH_FILE` points at its `auth.json`.
- [ ] Restart the host worker. A codex model account routes an agent run, and the run's activity shows `bsvibe_work_*` tool calls only.
- [ ] A codex account with `model: gpt-5.5` gets a clear refusal naming `apply_patch`.
- [ ] Re-measure on a codex upgrade. If a native item (`command_execution` / `file_change`) ever appears, the session aborts with `codex_native_action_refused` in the worker log.

## Automated cover

- `tests/executors/worker/test_codex_acts_through_bsvibe_tools.py` (12):
  - refusal without a surface;
  - every flag;
  - exact allowlist key;
  - token only in env;
  - isolated home and login copy (0600), removed after the run;
  - missing login;
  - model pin and refusal;
  - native-action abort;
  - chat turn with everything off.
- `tests/dispatch/test_executor_remote_tools.py`: `supports_remote_tools("codex")` is now True.
- 11 production edges were each cut once, and each cut turned a test red.
