# BSVibe session handoff — 2026-10-06

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

> 🐙 **Opening an issue or a PR on `BSVibe/bsvibe-app` now starts a prod run**. To file
> record-only issues/PRs, pause the binding filter first (§Ⅳ) — and ask 형님 each time.

---

## §0 — One line: **the run state machine is one function with a table; notifications say what happened**

| PR | Issue | What | Prod check |
|---|---|---|---|
| #1119 | #1106 · #1104 · #1114 | cancel kills the executor session · in-session token budget · `--max-turns` | ✅ cancel 4 s · budget kill at 15k · `--max-turns 60` |
| #1121 | #1103 | adjacent same-stage steps merge · TDD unit is one step · executor-neutral step handoff | ✅ one run carried test + fix, 86k input |
| #1122 | #1114 | a failed round hands its report + changed files to the next round | ⏳ a run that fails verification once |
| #1123 | #1102 | `transition()` is a compare-and-set | ⏳ CI ran it on Postgres |
| #1124 | #1105 | `run_token_cap_reached` gets a question + "예산 늘려 계속" | ⏳ next cap stop |
| #1125 | #1108 | next chain step keeps `binding_id` · `kind` → delivery gate survives | ⏳ a real cross-stage split |
| #1127 | #1110 | **every run status change goes through `run_status.move_run_status`** (AST guard) | ✅ BStockReport weekly run 10-05 reached review_ready normally |
| #1128 | #1109 | transition table (shipped is final) · **GitHub runs ship on MERGE** (awaiting_merge, PrConcluded callback, cap exclusion) | ⏳ next GitHub delivery |
| #1129 | #1074 | model-account Decisions ask + offer accounts + notify · **guard: every Decision kind has a question and a way out** | ⏳ next unresolved account |
| #1131 | (inventory) | frame-unresolved run no longer parks without a Decision · account lookup writes no Decision · bundle publish conflict is a report | ⏳ rare paths |
| #1132 | #1111 | **notifications split:** `review_ready` (approval card, at verify) · `shipped` (when the run ships, no buttons); old matrices inherit `shipped` | ✅ prod prefs read whole, `review_ready` inherited `true` |
| (next) | #1115 | a run whose last Safe Mode item ends undelivered (cleanup deny · reasonless deny · expiry) is cancelled — frees the cap slot | ⏳ next cleanup deny |

Checklists: `docs/e2e/*` — the unchecked boxes are the ⏳ column. **#1104 stays open on purpose** (no raw cache columns).

## §Ⅰ — Decisions still in force (형님, 2026-10-01 · 10-04)

| | |
|---|---|
| **Wrapping stays** | Claude Code and the files live on different machines; built-in tools stay off, the MCP work tools are the surface |
| **No `--resume`** | Continuity is BSVibe's. Every hand-over (#1103 step, #1114 round) is plain text on the payload / messages that any executor can read |
| **Shipped only after merge** (10-04) | A GitHub-delivered run waits at `review_ready` (`awaiting_merge`) until its PR merges; closed unmerged → `cancelled`. Runs with no merge watch row still ship on open. Awaiting-merge runs do not hold a concurrent-run slot |

## §Ⅱ — What this session found that the issues had wrong

* **#1114's premise was inverted.** It said "the whole conversation is re-rendered every round". The code
  dropped the round's report entirely: the next session got the seed context plus `Verification FAILED`
  and nothing about what the previous session did. #1122 fixes the real gap.
* **The run token cap is not wired through compose.** `BSVIBE_AGENT_MAX_RUN_TOKENS` in `.env.prod` never
  reaches a container. To lower it for a test, recreate **only the worker** with an override file that
  sets `worker.environment` (the cap check and the budget dispatch both run in the worker container), then
  recreate it again without the override.
* **The frame log does not record the step plan**, and run payloads are not exposed over MCP — #1103's prod
  check was judged by outcome (one run, both files, no `handoff_next_step_spawned`).

## §Ⅲ — Next

1. Prod walks in the ⏳ column. DB reads are 형님's (agents are refused prod reads by design).
2. `human_review_required` has no creator left — kept mapped on purpose (pending prod rows would go blank).
3. The #1115 backlog that predates the fix (runs whose items were already denied/expired) is not
   back-filled — 형님 decides whether to clear it once.
4. #1107 · #1112 · #1113 · #1116 · carry-overs.
5. Unexamined since 10-02: prod logs `supabase_token_failed` ~every 62 s from this host's IPv6.

## §Ⅳ — Discipline that paid off

* **🐙⭐⭐ Every PR on bsvibe-app is a prod action — pause, act, WAIT, check, restore.**
  `bsvibe_bindings_update c50e7217… {"filters":{"github_event":"__paused__"}}` → open / merge / push →
  **wait ≥60 s** → `bsvibe_runs_list` shows no new run → `{"filters":{}}`.
  ⚠️ The filter is applied when the **intake worker** processes the stored event, not when the webhook
  arrives. On 10-05 the filter was restored 3.6 s after the webhook for docs PR #1130 landed; intake ran
  after the restore and opened run `f26ef907` (32k tokens, a needs_you question to 형님) — cancelled. A
  `runs_list` right after the action proves nothing: the run does not exist yet. (This section used to
  say "restore right after opening" — that was the bug.)
* **🔪⭐⭐ A wire-cut must hit the wire you mean.** `s.replace(old, new, 1)` on
  `written_paths=written_paths,` changed the FIRST of five occurrences (the token-cap call), the test
  stayed green, and a false explanation got written into a test comment. `assert s.count(old) == 1` with
  enough surrounding lines. Skill: `a-cut-that-does-not-compile-is-not-a-wire-cut` §5.
* **🧮⭐ The collected-test count is the cheapest sensor.** A cut run printed "no tests ran" — zsh does not
  word-split `$T`, so pytest got one bogus path. Write the paths out.
* **🪞⭐ A race test must hold what production holds.** The identity map is weak: a discarded
  `await session.get(...)` is collected and the next `get` reloads from the DB, hiding #1102's race.
  The drive loop holds `run`; the test has to too.
* **🗑⭐⭐ `tests/_support.db_engine` DELETEs every row of whatever `BSVIBE_DATABASE_URL` names.** Pointing it
  at `devcontainer-postgres-1` (old `wt/phase-1-knowledge`) emptied that DB. PG verification = CI's
  `lint-and-test` (pgvector:pg16) or a throwaway container. Never an existing DB.
* **🐙⭐ Pushing to an OPEN PR is a prod action too.** It sends `pull_request synchronize` — pause the filter
  around the push exactly like opening or merging.
* **🧪⭐ RLS hides what SQLite shows.** A callback the merge watch calls inside `workspace_scope` returned
  "run missing" in CI's Postgres when the test called it unscoped — call callbacks the way the worker does.
* **🎯⭐ A guard on "non-empty text" cannot see a missing translation** — `_question_text` falls back to
  English. Assert `ko != en`.
* **🔐 Agents cannot read prod containers or the prod DB** (`docker exec` / `inspect` / `psql` are refused
  as production reads). `docker --context colima logs` works and is where the drive loop's events are
  (`bsvibe-prod-worker-1`); `backend-1` carries the MCP / API side.
* **🐳 Default docker context is `colima-palworld`; prod is `colima`.** Pass `--context colima`; never
  `docker context use`.
* **📏 `_drive_loop.py` is at 581 / 600** (`test_h2a_decomposition`). New loop logic goes to `_loop_turn.py`.
