# E2E — the executor really gets BSVibe's tools (and only those)

Unit tests cannot see any of this: every item below failed in production while CI was green.
Verify against **prod** (`api.bsvibe.dev`, demo workspace `5fa3494c-74cf-4d5a-b92b-6054f5684d07`)
after merge + deploy + `launchctl kickstart -k gui/501/com.bsvibe.worker`.

## The tool surface the CLI is handed

- [ ] A run-scoped task token's `tools/list` returns **exactly 9** tools, all `bsvibe_work_*`
      (was 86).
- [ ] The same token calling a workspace-wide tool (`bsvibe_products_list`) is **refused**
      (was: returned the workspace's products).
- [ ] The founder's ordinary MCP token still lists the full surface (no regression).

## The agent actually receives them

- [ ] The CLI's `system/init` reports `mcp_servers: [{status: "connected"}]` — **not**
      `pending` (this is the whole bug: `MCP_CONNECTION_NONBLOCKING` unset ≠ false).
- [ ] `system/init` exposes all 9 `mcp__bsvibe__bsvibe_work_*` tools and **zero** natives.
- [ ] The agent's first action is a real `mcp__bsvibe__…` tool call — not prose containing
      `<invoke name="glob">`, and not "the directory appears to be empty".

## The guard fails loudly, never silently

- [ ] With the MCP server unreachable (revoke the token), the task **aborts** with
      "BSVibe's tools never arrived" instead of returning a fabricated answer marked success.

## The run does real work

- [ ] Drive from the PWA / `bsvibe_direct`: *"backend/common 에 clamp(v, lo, hi) 헬퍼와 테스트
      추가해줘"*.
- [ ] `mcp_work_tool` lines appear in **both** worker and backend logs.
- [ ] The files land in the run's **server-side** worktree (`/app/var/runs/<run_id>/`), written
      by `file_write` — no `_collect_workspace_files` scrape.
- [ ] Verification (pytest/ruff) runs in the sandbox via `shell_exec`.
- [ ] The **host source repo** (`~/Works/bsvibe-app/main`) is untouched — `git status` matches
      its pre-run baseline. (The launchd worker's cwd IS that repo; the CLI having no local
      file tools is the only thing keeping the agent out of it.)
