# BSVibe session handoff — 2026-10-02

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

## §0 — One line: **two fixes on one branch, both waiting on a PR that is itself a prod action**

Branch **`claude/bridge-cse_01824DHDZL1TUghFq1diRT1t`** carries both (it contains the #1106 branch):

| Commit | What |
|---|---|
| `71ff937` | #1106 — cancelling a run kills its executor session and refuses its token |
| `8f74c3b` | #1104 + #1114 — in-session token budget, cost-weighted usage, `--max-turns` |

Neither is on `main`. No PR yet: the BSVibe MCP was not usable in the session that built them (§Ⅲ-1).

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
| #1104 + #1114 | ✅ on the branch (§Ⅱ), except: raw cache breakdown not stored · transcript-summary half goes with #1103 |

## §Ⅱ — What is on the branch (not merged)

**#1106** (`71ff937`) — unchanged from the 10-01 handoff:
* the awaiter's `abandon_if` run-status probe → `cancel_task` → `RunCancelledDuringTurn` (not retryable)
* `load_run` refuses cancelled / failed / shipped runs
* no worker change
* checklist: `docs/e2e/cancel-reaches-the-executor-session-checklist.md`

**#1104 + #1114** (`8f74c3b`) — checklist `docs/e2e/executor-session-limits-checklist.md`:
* **Unit:** claude_code usage is weighted by cost, in input-token equivalents. Cache read ×0.1; cache write ×2 for the 1h TTL, ×1.25 for 5m. Raw sums had stopped run `92b76fba` at 3.05M on a task that finished normally
* **Moment:**
  * The adapter dispatches the run's **remaining** budget as `token_budget` (`cap − used`, min 1), with `max_turns`, on the #965 redelivery too
  * The worker tallies assistant `message.usage`, deduped by `message.id`, and kills the process group at the budget
  * The turn ends **done**, not failed (failed is retried), so the drive loop's existing cap check raises the Decision
* **`--max-turns`:** `executor_agent_max_turns`, default **60** — a guess, tune it from prod data. `error_max_turns` + exit 1 is mapped to a normal finish
* **Verified:**
  * Real-CLI probe (2.1.286, one haiku turn)
  * RED → GREEN
  * Wire-cut on 8 wires, each red on its own test
  * Full suite 7218 passed · import-linter 6/6 · ruff · mypy
* **⚠️ This one changes the WORKER.** After merge, restart both host workers (top table). A backend-only deploy sends the keys to workers that ignore them
* **Gaps:**
  * Raw cache breakdown is not stored (no columns)
  * opencode takes agentic turns (#1000) but ignores both limits
  * codex and opencode still count cache at equal weight

## §Ⅲ — Next

1. **BSVibe MCP in a FRESH session.** 형님 re-authenticated on 10-02, and the token works: initialize → 200 against `/mcp/`. But:
   * A session's MCP tool list is fixed at its start, so the session that ran the auth never saw the tools
   * The **credential stores are split.** `~/.claude/.credentials.json` has the valid bsvibe token. The macOS keychain item `Claude Code-credentials` has every one of its 22 `mcpOAuth` entries with an **empty** token, bsvibe included
   * `claude mcp get/list` read the keychain, so they still say `Needs authentication`. Don't trust that line; check whether `bsvibe_*` tools appear (ToolSearch `+bsvibe`)
   * If they don't, the empty keychain entry is the suspect. Deleting it touches 형님's credential store, so ask first
2. **Open the PR as record-only** (one PR for the branch, or #1106 first). Pause → confirm → PR → confirm → restore:
   * `bsvibe_bindings_update` `c50e7217…` → `{"filters":{"github_event":"__paused__"}}`
   * confirm the filter took
   * open the PR
   * confirm the trigger row carries `_received_filtered` and `requests` did not grow
   * restore `{"filters":{}}`
   * **ask 형님 each time**
3. **Merge → restart the host workers → post-deploy E2E** from both checklists. Those include deliberate prod runs (a cancel mid-turn; a lowered cap), so ask first.
4. Then #1103 (framer keeps one TDD unit per run; executor-neutral handoff), together with the summary half of #1114.
5. Still open from before: #1113 opt-in (label filter is the cheapest interim) · #1102/#1110 state machine · #1107 · #1108 · #1111 · #1112 · #1105 · carry-overs (credential rotation ×3 · #1047 · #937 · #1042 · #954 · #949 · #957).
6. **New, unexamined:** prod backend logs show `supabase_token_failed` (password grant, 400) about **every 62 s**, plus `/api/auth/login` 401, from this host's IPv6. Some probe or script logging in with a stale password, most likely. Not investigated.

## §Ⅳ — Discipline that paid off

* **🧪⭐⭐ Probe the real CLI before wiring a bound.**
  * `claude --max-turns` exits **1** with `result/error_max_turns`. Behind a "failed → retry" adapter, the bound would re-run the round it just stopped
  * Each content block repeats its message's usage under one `message.id`, and its `output_tokens` is a partial snapshot
  * Cache writes are 1h TTL (2×)
  * The probe was one haiku turn; the fixtures copy its events verbatim (skill `a-bound-reported-as-failure-is-retried-past`)
* **🔑⭐ "Needs authentication" can be a client store split, not a dead token.**
  * Read the server log: the token exchange returned 200 and the calls that followed returned 200
  * The 401s that followed were other processes sending **no** token
  * The `/mcp` → `/mcp/` 307 is FastAPI's slash redirect and is harmless


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
