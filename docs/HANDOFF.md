# BSVibe session handoff — 2026-10-01

**Deploy topology** — they differ. Mixing them up costs an hour of "I deployed, why didn't it change".

| | How it ships |
|---|---|
| **Container stack** (backend · worker) | ✅ **autodeploy every 2 min** — a **separate block** in `_infra/scripts/autodeploy.sh` (line ~171). It is not in the `PROJECTS` array (easy to misread as "not deployed"). State: `_infra/logs/bsvibe-app.deployed` |
| **PWA** | Vercel on merge to main |
| **Host executor workers (2)** | 🔴 **not autodeployed.** `git pull` on `main`, then `launchctl kickstart -k gui/501/com.bsvibe.worker-{admin,mac-mini-e2e}` and check the **process start time**. If you changed the plist: `bootout` + `bootstrap` |

⚠️ **A PR that touches both frontend and backend opens a window** — Vercel ships at once, the backend poller every 2 min.

**Open work lives in GitHub issues, not in this document.**
**How the whole system works: [SYSTEM_OVERVIEW.md](./SYSTEM_OVERVIEW.md)** — read §4 before touching anything about executors.

> 🧭 **Never put anything transitive in this header.** A field that points at itself becomes
> false the moment it merges — updating the value does not fix it; **removing the field** does.

> 🐙 **Opening an issue or a PR on `BSVibe/bsvibe-app` now starts a prod run** (§Ⅱ). To file
> record-only issues/PRs, pause the binding filter first (§Ⅳ) — and ask 형님 each time.

---

## §0 — One line: **#1106 is fixed on a branch; it is waiting on a PR that is itself a prod action**

형님 settled the central design question from the last handoff: **BSVibe keeps wrapping Claude
Code the way it does now** (built-ins off, MCP work tools only). #1106 (cancel does not stop the
session) is fixed on `claude/bridge-cse_01UcAKMcioBCqfkBrsKF8adZ` (commit `71ff937`). No PR yet,
because opening one starts a prod run and this session had no sanctioned way to pause the
binding filter.

## §Ⅰ — Decisions (형님, 2026-10-01)

| | |
|---|---|
| **Wrapping stays** | Claude Code and the real files live on **different machines**. Files may sit on the BSVibe server while a local Claude Code connects to them. So built-in tools are out; the MCP work tools are the surface. Don't propose "let Claude Code use its own tools" again |
| **No `--resume`** | Continuity belongs to **BSVibe, not the executor**. A user may split work so the next step runs on codex or another executor, and a Claude Code session file cannot cross that boundary. Make BSVibe's handoff between rounds and steps smaller and executor-neutral instead |

How the open issues resolve under this:

| Issue | Direction |
|---|---|
| #1114 | `--max-turns` only. Replace the full re-rendered transcript with a BSVibe-owned summary (pairs with #1103) |
| #1103 | The framer keeps one TDD unit (test + fix) in one run. When it does split, it leaves a handoff any executor can read: explored, touched, verify state |
| #1104 | Weight cache reads by real cost; enforce **mid-session** from stream-json usage, then kill the process |
| #1106 | ✅ on the branch (§Ⅱ) |

## §Ⅱ — #1106 on the branch (not merged)

* **Session kill:** `await_completion` takes an `abandon_if` probe, asked every poll tick after the terminal read. `ExecutorAdapter` passes a run-status probe for run-bound turns. On cancel it does three things: closes the task row (`failed`, `abandoned:`), sends `cancel_task` to the worker, and raises `RunCancelledDuringTurn` (not retryable)
* **Drive loop:** `_loop_turn.take_turn` ends both the between-turns cancel and the mid-turn cancel as "cancelled". It was split out because `_drive_loop.py` sat at exactly the 600-LOC guard
* **Token refusal:** `load_run`, the gate every work tool passes through, refuses cancelled / failed / shipped runs
* **No worker change.** The Lift E14 cancel handling is reused, so backend autodeploy is enough
* **Verified:**
  * RED → GREEN, plus a wire-cut on each of the three call-site wires (each one turns its own test red, collection intact)
  * Full suite 7203 passed · import-linter 6/6 · ruff · mypy
* **Checklist:** `docs/e2e/cancel-reaches-the-executor-session-checklist.md` (post-deploy items unchecked)
* **Known gaps:**
  * The degraded pure-DB poll (pub/sub failed) has no probe
  * A backend restart mid-wait loses the kill, though the token refusal still holds

## §Ⅲ — Next

1. **Authenticate the BSVibe MCP.** It was added at **user scope** this session (`claude mcp add --scope user bsvibe https://api.bsvibe.dev/mcp`), so every worktree sees it. It shows `Needs authentication` until `/mcp` is run once in an interactive session.
2. **Open the #1106 PR as record-only.** Pause → confirm → PR → confirm → restore:
   * `bsvibe_bindings_update` `c50e7217…` → `{"filters":{"github_event":"__paused__"}}`
   * Confirm the filter took
   * Open the PR
   * Confirm the trigger row carries `_received_filtered` and `requests` did not grow
   * Restore `{"filters":{}}`
   * 형님 OK'd this on 10-01, but ask again in the new session
3. **Merge, then run the post-deploy E2E** from the checklist. That means starting a run on purpose and cancelling it after its executor task is `dispatched`:
   * expect `executor_adapter_run_cancelled_mid_turn` → `dispatch_cancel_xadd_succeeded` → `worker_task_cancel_started`
   * expect the task row `failed`/`abandoned:`
   * expect no accepted `mcp_work_tool` calls afterwards
   * This is a prod run, so ask first
4. Then #1104 + #1114 together (both in `backend/executors/worker/claude_code.py`), then #1103.
5. Still open from before: #1113 opt-in (label filter is the cheapest interim) · #1102/#1110 state machine · #1107 · #1108 · #1111 · #1112 · #1105 · carry-overs (credential rotation ×3 · #1047 · #937 · #1042 · #954 · #949 · #957).

## §Ⅳ — Discipline that paid off

* **🐙⭐⭐ Filing issues/PRs on bsvibe-app is now a prod action.** Record-only: set the binding filter to a non-matching value (`bsvibe_bindings_update`, `{"filters":{"github_event":"__paused__"}}`), confirm the trigger row carries `_received_filtered` and `requests` did not grow, then restore `{"filters":{}}`. The auto-mode classifier blocks the binding change unless 형님 OKs it in the conversation.
* **🛑⭐⭐ Cancelling a run does not stop its session.** A run cancelled at 10:41 kept its Claude Code session alive until 10:52 (6.4M tokens) with its run-scoped MCP token still valid (#1106). Cancel only before the executor task exists (~25 s after the request), or expect to pay.
* **🔬⭐⭐ Canary with a positive control, on the same snapshot.** A "no change" canary is only evidence if the known-bad code turns it red on that very dump.
* **🧾⭐ Re-verify subagent reports before acting.** Of the audit claims turned into issues, one was wrong ("`output_mode` has no reader" — `delivery_worker` reads it; the real defect was #1108).
* **🪞 A source-text guard matched my own docstring.** Cutting the call left the guard green because the helper's docstring named the function; narrowed to the call form `_resolve_inbound_product(`.
* **🗄 `SELECT DISTINCT` over a JSON column fails on Postgres** (`could not identify an equality operator for type json`) — SQLite never tells you. Use `IN (subquery)`.
* **🧯 Nothing was committed in the deploy directory.** Every change went through a worktree.
* **🔐 This session could not reach prod, by design.**
  * A `docker --context colima exec bsvibe-prod-postgres-1 psql …` read was refused by the auto-mode classifier as a production read
  * Self-adding a settings allow rule for it was refused too
  * Use the BSVibe MCP (§Ⅲ-1); don't route around the classifier
* **🐳 The default docker context on this host is `colima-palworld`.** The prod stack is on `colima`. Pass `--context colima`; never `docker context use`, because sibling scripts depend on the current context.
* **📏 `_drive_loop.py` was at exactly 600 lines** (`test_h2a_decomposition`). Any addition there needs a sub-split. `_loop_turn.py` is the precedent and is registered in that guard.
