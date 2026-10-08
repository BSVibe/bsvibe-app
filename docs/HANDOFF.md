# BSVibe session handoff — 2026-10-08

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

## §0 — One line: **work from outside meets the same limits as work from 형님; ship opens the PR**

10-06 → 10-07 (this session):

| PR | Issue | What | Prod check |
|---|---|---|---|
| #1133 | #1115 | a run whose last Safe Mode item ends undelivered is cancelled — frees the cap slot | ⏳ next cleanup deny |
| #1134 | (log noise) | `GET /api/health/auth` relays GoTrue `/auth/v1/health`; offbox probe + heartbeat (workstation #10) read it instead of a bogus login | ✅ 200 via real Supabase · `supabase_token_failed` gone |
| #1135 | #1107 | **one sandbox container per RUN** (`bsvibe-sbx-<run id>`) · a live box of the same run + worktree is adopted, not `rm -f`-ed (worker ↔ API) · slots of boxes removed elsewhere are freed | ✅ `bsvibe-sbx-<run id>` per run (runs `4414bcd5`, `b2ebd3c4`) · ⏳ `sandbox_adopted` not yet seen |
| #1136 | #1112 | "승인하고 출시" on a GitHub product writes the delivery event (`founder_approved` → skips Safe Mode), no local force-merge, run stays review_ready → PR → ships on merge | ⏳ next ship on a BSVibe run |
| #1137 | #1113 | **webhook work at intake:** run cap full → held (`_intake_held`, released oldest-first per free slot) · monthly token budget spent → refused + `needs_you` · **claim excludes refused/held rows in SQL** | ⏳ held / released / refused log lines |
| #1138 | #1116 | tool-menu guard (every `bsvibe_*` / `executor/<x>` a description names must exist) · webhook skips client_attach `run/<8hex>` PR branches | ⏳ next BStockReport PR makes no run |
| #1139 | (tests) | two order-dependent tests fixed (unrestored module patch · idempotence counted as a delta) | ✅ whole suite in one process, 6732 passed |
| #1141 | (measured) | the server-side gate derives commands only over changed paths still present — a removed scratch file no longer yields `ruff … E902` | ✅ run `b2ebd3c4` passed with 3 removed `_patch_*.py` |
| #1147 | #1143 | the merge watch's squash sends `commit_title` = PR title + ` (#N)` — main no longer gets the run commit (`work: <directive first line>`) | ⏳ next auto-merge |
| #1148 | #1144 | a Direct run's PR body quotes the founder's directive (`**요청**`) and links every `#N` it names with a non-closing `Refs` (issue-sourced runs keep `Closes #N`) | ⏳ next Direct run PR |
| workstation #11 | (#1145 prereq) | autodeploy archives backend/worker logs to `_infra/logs/bsvibe-prod/<container>--<started>.log` before `--force-recreate` (20 kept) | ⏳ open — 형님 merges; live already (launchd runs the `_infra` tree) |
| #1142 | #1073 | **written by BSVibe itself:** a rule whose target has no account is skipped (`routing_rule_target_missing`) → next rule → default; the runtime keeps the account the fallback found | ✅ CI green, auto-merged, run shipped |

**Measurement 10-07 — #1073 handed to BSVibe** (`bsvibe_direct`, product `bsvibe`):

| Run | Outcome | Tokens in/out | Why |
|---|---|---|---|
| `4414bcd5` | stopped on a question after 2 failed verifies (15 min) → discarded | 358k / 28k | gate linted a scratch file the agent had removed (E902) → #1141 |
| `b2ebd3c4` | verified in 8 m 48 s → approved → PR #1142 → auto-merged → shipped | 379k / 24k | — |

Found by it: #1143 (auto-merge leaves the run commit message — the directive's first line — on main) · #1144
(PR body has no `Closes #N`, no why) · #1145 (the agent writes scratch `_patch_*.py` at the repo root, twice).
The own-PR skip held (#1142 made no run). A run that stops on a QUESTION makes no status transition —
watch `agent_worker_driven`, not transitions.

Checklists: `docs/e2e/{run-with-nothing-to-deliver,auth-health-probe,sandbox-per-run,ship-on-github-product,intake-gates-webhook-work,tool-menu-and-own-branches}-checklist.md`.

10-05 → 10-06 (previous session) — **the run state machine is one function with a table; notifications say what happened**:

| PR | Issue | What | Prod check |
|---|---|---|---|
| #1119 | #1106 · #1104 · #1114 | cancel kills the executor session · in-session token budget · `--max-turns` | ✅ cancel 4 s · budget kill at 15k · `--max-turns 60` |
| #1121 | #1103 | adjacent same-stage steps merge · TDD unit is one step · executor-neutral step handoff | ✅ one run carried test + fix, 86k input |
| #1122 | #1114 | a failed round hands its report + changed files to the next round | ⏳ a run that fails verification once |
| #1123 | #1102 | `transition()` is a compare-and-set | ⏳ CI ran it on Postgres |
| #1124 | #1105 | `run_token_cap_reached` gets a question + "예산 늘려 계속" | ⏳ next cap stop |
| #1125 | #1108 | next chain step keeps `binding_id` · `kind` → delivery gate survives | ⏳ a real cross-stage split |
| #1127 | #1110 | **every run status change goes through `run_status.move_run_status`** (AST guard) | ✅ BStockReport weekly run 10-05 reached review_ready normally |
| #1128 | #1109 | transition table (shipped is final) · **GitHub runs ship on MERGE** (awaiting_merge, PrConcluded callback, cap exclusion) | ✅ run `b2ebd3c4`: PR #1142 → review_ready + awaiting_merge → merge watch auto-merged → shipped 2 s later |
| #1129 | #1074 | model-account Decisions ask + offer accounts + notify · **guard: every Decision kind has a question and a way out** | ⏳ next unresolved account |
| #1131 | (inventory) | frame-unresolved run no longer parks without a Decision · account lookup writes no Decision · bundle publish conflict is a report | ⏳ rare paths |
| #1132 | #1111 | **notifications split:** `review_ready` (approval card, at verify) · `shipped` (when the run ships, no buttons); old matrices inherit `shipped` | ✅ prod prefs read whole, `review_ready` inherited `true` |

Checklists: `docs/e2e/*` — the unchecked boxes are the ⏳ column. **#1104 stays open on purpose** (no raw cache columns).

## §Ⅰ — Decisions still in force (형님, 2026-10-01 · 10-04)

| | |
|---|---|
| **Wrapping stays** | Claude Code and the files live on different machines; built-in tools stay off, the MCP work tools are the surface |
| **No `--resume`** | Continuity is BSVibe's. Every hand-over (#1103 step, #1114 round) is plain text on the payload / messages that any executor can read |
| **Shipped only after merge** (10-04) | A GitHub-delivered run waits at `review_ready` (`awaiting_merge`) until its PR merges; closed unmerged → `cancelled`. Runs with no merge watch row still ship on open. Awaiting-merge runs do not hold a concurrent-run slot |
| **#1115: plug the leak only** (10-06) | Undeliverable runs are released; the cap still counts `review_ready`. The pre-fix backlog is not back-filled (checked 10-06: one non-terminal run, `1c5290f3`, a legitimate wait) |
| **Every GitHub issue / PR / comment is work** (10-06) | No label or command opt-in — "일단 받고, 단순 질의거나 의미 없으면 종료된다". Webhook work: **cap → wait, budget → refuse** |
| **Ship is the approval** (10-06) | "승인하고 출시" does not raise a second Safe Mode card |
| **One sandbox per run** (10-06) | `sandbox_max_concurrent=2` now counts runs; a third server_sandbox run waits for a slot |

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

## §Ⅱ-b — What 10-06 found

* **`supabase_token_failed` every ~62 s was our own probe**, not an attack: `heartbeat.sh` (launchd, 60 s)
  and `offbox-uptime.yml` (15 min) POSTed a bogus password to `/api/auth/login` to read "is Supabase up".
  Every reading was a failed sign-in on the per-IP limit every user's login shares. → #1134.
* **Intake had a time bomb.** `list_undrained` took the oldest 50 triggers without a Request and only then
  skipped filter-rejected ones in Python. Rejected triggers never get a Request, so 50 of them (one per
  paused record-only PR) would have stopped intake for good — direct submissions included. → #1137.
* **Two processes drive one DinD** (worker `RunOrchestrator` + API MCP work tools) with separate caches; the
  API's first tool call used to `rm -f` the worker's box. Per-run keys alone do not fix that — adoption does.
* **launchd runs `_infra`'s working tree.** Editing `heartbeat.sh` on a branch changes prod at once; change
  it only after the backend it calls is deployed.

## §Ⅲ — Next

1. Prod walks in the ⏳ column (both tables). DB reads are 형님's (agents are refused prod reads by design).
2. `human_review_required` has no creator left — kept mapped on purpose (pending prod rows would go blank).
3. Still outside the caps on purpose: schedule ticks and next-step spawns (`run_caps` docstring).
4. Held webhook triggers are invisible in the PWA (logs only) — a `triggered` notification goes when released.
5. Branch rules are still three (`bsvibe/run/<uuid>` · `bsvibe/run-<8hex>` · `run/<8hex>`); #1116 taught the
   skip all three instead of renaming 형님's worktrees.
6. #1145 — cause unconfirmed (hypothesis: `file_edit` refuses a path not `file_read` first; the agent read via
   shell and patched with scripts). Logs now survive deploys — confirm on the next measured run.
7. Next cluster (형님 10-08): **schedules & reports** — #1077 → #1079 → #1078 → #1072 (+ #673).
8. Carry-overs in GitHub issues.

## §Ⅳ — Discipline that paid off

* **🐙⭐⭐ Every PR on bsvibe-app is a prod action — pause, act, WAIT, check, restore.**
  `bsvibe_bindings_update c50e7217… {"filters":{"github_event":"__paused__"}}` → open / merge / push →
  **wait for intake's own verdict** (`docker --context colima logs --since 2m bsvibe-prod-worker-1 | grep
  filter_rejected`; a merge sends no event — wait ≥90 s) → `bsvibe_runs_list` shows no new run →
  `{"filters":{}}`. Held 7 PRs in a row this session.
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
* **🔪⭐ Never wire-cut while a suite runs in the background.** Cuts rewrite source files; a background run
  imports whatever is on disk at that moment. Stop it, cut, restore, then rerun.
* **🧪⭐ A failure only in a wide run is pollution until shown otherwise.** Bisect by file pairs, then
  reproduce on a clean `origin/main` worktree (reuse the venv with `.venv/bin/python -m pytest` from the
  worktree dir — `backend` resolves from cwd). Raw `mod.X = fake` in a test helper is the usual culprit.
* **🐙⭐ Issue COMMENTS are prod actions too.** On 10-07 the filter was restored, then `gh issue close 1073
  --comment …` 11 s later became run `bf316afc` (68k tokens, review card to 형님) — discarded. Do every
  issue/PR write (create · comment · close-with-comment · review) inside ONE pause window.
* **🤖 The merge watch merges on its own** once CI is green (founder's token). A manual `gh pr merge` after that
  is a no-op — check `mergedAt` before reading the timeline.
* **🐘 Throwaway Postgres for JSON-path SQL:** `docker --context colima run -d --rm … pgvector/pgvector:pg16`,
  then `CREATE EXTENSION vector` before the schema is created. `payload[KEY].as_string().is_(None)` behaves
  the same on PG and SQLite (`->>` / `json_extract`).
