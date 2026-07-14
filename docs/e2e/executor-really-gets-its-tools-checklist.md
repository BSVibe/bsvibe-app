# E2E — the executor really gets BSVibe's tools (and only those)

Unit tests cannot see any of this: every item below failed in production while CI was green.

**DRIVEN AGAINST PROD 2026-07-14, all green** — prod `7c0d508` (PR #558), demo workspace
`5fa3494c-74cf-4d5a-b92b-6054f5684d07`, run `96dd7cfc-ad5b-42dc-9492-afe780af79f0`
(*"backend/common 에 clamp(value, lo, hi) 유틸 함수와 단위 테스트를 추가해줘"* → **shipped in 2m40s**).

## The tool surface the CLI is handed

- [x] A run-scoped task token's `tools/list` returns **exactly 9** tools, all `bsvibe_work_*`
      (was 86).
- [x] The same token calling a workspace-wide tool (`bsvibe_products_list`) is **refused**
      (was: returned the workspace's products).
- [x] The founder's ordinary MCP token still lists the full surface (no regression).

## The agent actually receives them

- [x] The CLI's `system/init` reports `mcp_servers: [{status: "connected"}]` — **not**
      `pending` (this is the whole bug: `MCP_CONNECTION_NONBLOCKING` unset ≠ false).
- [x] `system/init` exposes all 9 `mcp__bsvibe__bsvibe_work_*` tools and **zero** natives.
- [x] The agent's first action is a real `mcp__bsvibe__…` tool call — not prose containing
      `<invoke name="glob">`, and not "the directory appears to be empty".

## The guard fails loudly, never silently

- [x] Absence aborts: the guard now demands the exposed set be EXACTLY ours (unit-covered;
      the live proof is that the *previous* build aborted loudly on `TaskCreate/TaskGet/
      TaskList/TaskUpdate` rather than running with tools we never sanctioned).

## The run does real work

- [x] Driven from `bsvibe_direct` against the demo workspace.
- [x] **24 `mcp_work_tool` calls** on the backend:
      `file_list ×5 → knowledge_search → file_read ×6 → shell_exec ×8 →
       declare_verification ×2 → file_edit → file_write`.
      Note the ordering: **`declare_verification` precedes every write** — the verify-first
      (TDD) gate is now genuinely enforced on the executor (audit gap #7).
      Note also: the agent **navigated a 1,500-file repo with `file_list`/`file_read` alone**
      — settling design open-question #1. Server-side `grep`/`glob` were not needed.
- [x] The files landed in the run's **server-side** worktree
      (`/app/var/runs/<run_id>/backend/common/clamp.py`, `tests/common/test_clamp.py`) via
      `file_write`/`file_edit` — **no `_collect_workspace_files` scrape**.
- [x] Verification ran in the sandbox: `sandbox_created → verified_deliverable_written →
      run_orchestrator_verified → sandbox_removed`. A `code` deliverable was emitted.
- [x] The **host source repo** (`~/Works/bsvibe-app/main`) is byte-identical to its pre-run
      baseline. (The launchd worker's cwd IS that repo; the CLI having no local file tools is
      the only thing keeping the agent out of it.)
