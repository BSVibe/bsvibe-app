# BSVibe session handoff — 2026-10-21

**Deploy topology** — they differ. Mixing them up costs an hour of "I deployed, why didn't it change".

| | How it ships |
|---|---|
| **Container stack** (backend · worker) | ✅ **autodeploy every 2 min** — a **separate block** in `_infra/scripts/autodeploy.sh` (line ~171). It is not in the `PROJECTS` array (easy to misread as "not deployed"). State: `_infra/logs/bsvibe-app.deployed` |
| **PWA** | Vercel on merge to main |
| **Host executor worker (1)** | 🔴 **not autodeployed.** `git pull` in `~/Works/bsvibe-app/main`, then `launchctl kickstart -k gui/501/com.bsvibe.worker-mac-mini-e2e` and check the **process start time**. If you changed the plist: `bootout` + `bootstrap`. (`worker-admin` was retired 10-16 — its plist is in `~/.bsvibe/retired/`) |

⚠️ **A PR that touches both frontend and backend opens a window** — Vercel ships at once, the backend poller every 2 min.

**Open work lives in GitHub issues, not in this document.**
**How the whole system works: [SYSTEM_OVERVIEW.md](./SYSTEM_OVERVIEW.md)** — read §4 before touching anything about executors.

> 🧭 **Never put anything transitive in this header.** A field that points at itself becomes
> false the moment it merges — updating the value does not fix it; **removing the field** does.

> 🐙 **Opening an issue or a PR on `BSVibe/bsvibe-app` now starts a prod run**. To file
> record-only issues/PRs, pause the binding filter first (§Ⅳ) — and ask 형님 each time.

---

## §0 — One line: **a run can move between machines without losing its work; codex is on the MCP surface; cloud sessions see the rules**

10-19 → 10-21 (this session). Every GitHub write went pause → act → CI green → merge → no new run → restore (§Ⅳ).

| PR | Issue | What | Prod check |
|---|---|---|---|
| #1183 | #1000 | codex agent runs act only through the BSVibe MCP surface (`view_image` allowed, models pinned to ones without `apply_patch`); opencode measured | ✅ deployed · ⏳ **형님:** one live codex run |
| #1184 | #967 | an executor wait timeout says what it read in scope and what the row says unscoped — starvation vs an invisible row | ✅ deployed |
| #1185 | #961 | the PWA can schedule a product's autonomous check (`product_tick`); deleting a product removes its schedules | ✅ deployed |
| #1186 | #951 | cloud sessions get the rules: repo `CLAUDE.md` + a SessionStart hook (`.claude/hooks/install-skills.sh`) that installs `blas1n/claude-skills` — `docs/runbook/agent-context.md` | ✅ merged |
| #1187 | #929 | the OAuth rate limit is shared across backend replicas (Redis sliding window, Lua) | ✅ deployed |
| #1188 | #929 | a machine's product repo follows the bundle another machine shipped (fast-forward only) | ✅ deployed |
| #1189 | #929 | verify disk slots are per machine — **`BSVIBE_MACHINE_ID`** (empty = single box) | ✅ deployed |
| #1190 | #929 | a run's worktree (branch + uncommitted edits) is checkpointed to R2 before every claim release and restored on another machine; a run that cannot be restored is **refused**, never re-branched from `main` | ✅ deployed (inert: no machine id) |
| #1191 | #929 | `execution_runs.home_machine` — stamped on claim, cleared on every release | ✅ migration `run_home_machine` ran |
| #1192 | #929 | an MCP work-tool call for a run whose home is another machine is forwarded there (`bsvibe:machine:<id>` stream, at most once, deadline, refusal round-trips) | ✅ deployed, `mcp_lifespan_starting` clean (inert) |
| #1193 | #929 | a finished run's R2 checkpoint is swept (DB-driven: the store cannot list) | ✅ deployed, host worker restarted 10-20 |

Closed: **#1000**, **#967**, **#961**, **#951**. **#929** stage 1 of 4 done (`docs/design/stateless-workers.md`).
Everything #929 is gated on `BSVIBE_MACHINE_ID`; prod leaves it empty, so the single box behaves as before.

## §0-b — previous session (10-16 → 10-19): keys rotate, product secrets reach the server box, a delivered run ships whatever the order

10-16 → 10-19. Every GitHub write went pause → act → CI green → merge → deploy checked →
no new run → restore (§Ⅳ).

| PR | Issue | What | Prod check |
|---|---|---|---|
| #1172 | #949 (1) | `/api/v1/chat/completions` deleted — no consumer | ✅ |
| #1173 | #957 | credential ciphertext names its key (`kms1.<kid>.…`), `BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64` still decrypts, `python -m backend.router.accounts.rewrap [--apply]` re-encrypts; one factory (AST guard) | ✅ deployed, 0 decrypt errors in logs · ⏳ **형님:** rewrap dry-run in the backend container (agents are refused prod exec) |
| #1174 | #691 | a `server_sandbox` product's commands get its `verify_secrets` (`docker exec -e NAME`, value only in the CLI's env) — loop and MCP paths | ⏳ `docs/e2e/server-sandbox-secrets-checklist.md` |
| #1175 | #949 (2) | an agentic Claude Code turn's output is the CLI's final `result` text, not the joined narration | ✅ deployed, host worker restarted |
| #1176 | #1145 | `written_paths` re-read from the tree when the agent says done (tracked changes from git + still-existing write-tool files); MCP `file_edit` states its rules | ⏳ next agent run that edits through the shell |
| #1177 | #949 (4) | verification grades on the judge route (`workflow.judge`, unbound to the run), act LLM as fallback | ⏳ next verified executor run |
| #1178 | #1042 | activity: same-minute batches fold into one row, untitled runs say so · danger zone spaced | ✅ live stack (#1181) |
| #1179 | (형님 screenshots) | settings 알림/예약: standalone hints left-aligned full width, events list unbulleted, schedule form stacked, long instruction clamped | ✅ live stack red → green, screenshots |
| #1180 | (10-19 incident) | **`deliverables.delivered_at`** + `ship_if_delivered` at the REVIEW_READY transition and on every delivery-worker tick — a run whose output went out before REVIEW_READY ships | ✅ stranded weekly run `4f5e9bfb` shipped 03:20Z by the sweep, 0 re-sends |
| #1181 | #1042 §4 | settings tab no longer starts 34px lower than activity; floating button verified not to cover the last line | ✅ live stack 6/6 · merged |

Closed: **#957**, **#691**, **#1145**, **#949** (all four), **#1042** (with #1181).

**10-19 weekly report (BStockReport):** published once (one deliverable, verbatim block intact, no re-send loop
— #1160 held), but sat at REVIEW_READY: it was delivered while RUNNING, and delivery only ships a REVIEW_READY
run. → #1180, which also shipped that run. Re-walk on **10-26 09:30 KST**: it should end `shipped` with one
Telegram message and no human step.

## §Ⅰ — Decisions still in force (형님, 2026-10-01 · 10-04 · 10-16 · 10-19 · 10-20)

| | |
|---|---|
| **Open-ended free beta** (10-16) | Billing is **paused** (business registration). Everyone is on Free: 3 concurrent runs · 1 verification stack · 50M tokens/month. No trial period, no expiry. Nothing in the UI may say "upgrade" |
| **When billing resumes** (10-16) | Capacity subscription — tokens stay BYO, BSVibe sells platform capacity · Paddle as MoR · Pro = 10 runs · 2 stacks. Resume at step 3: `docs/design/billing-capacity-subscription.md` |
| **Wrapping stays** | Claude Code and the files live on different machines; built-in tools stay off, the MCP work tools are the surface |
| **No `--resume`** | Continuity is BSVibe's. Every hand-over (#1103 step, #1114 round) is plain text on the payload / messages that any executor can read |
| **Shipped only after merge** (10-04) | A GitHub-delivered run waits at `review_ready` (`awaiting_merge`) until its PR merges; closed unmerged → `cancelled`. Runs with no merge watch row still ship on open. Awaiting-merge runs do not hold a concurrent-run slot |
| **#1115: plug the leak only** (10-06) | Undeliverable runs are released; the cap still counts `review_ready`. The pre-fix backlog is not back-filled (checked 10-06: one non-terminal run, `1c5290f3`, a legitimate wait) |
| **Every GitHub issue / PR / comment is work** (10-06) | No label or command opt-in — "일단 받고, 단순 질의거나 의미 없으면 종료된다". Webhook work: **cap → wait, budget → refuse** |
| **Ship is the approval** (10-06) | "승인하고 출시" does not raise a second Safe Mode card |
| **One sandbox per run** (10-06) | `sandbox_max_concurrent=2` now counts runs; a third server_sandbox run waits for a slot |
| **codex and opencode go onto the MCP surface too** (10-19) | Same as claude_code, no exception kept (#1000). Re-measure both CLIs' current versions against the issue's two gates before wiring |
| **#929 = B: state lives in R2, any machine can take over** (10-20) | Not "pin a run to its machine". Stage 1 (run worktrees, tool calls) is done; **no second machine before stages 1–4 are live**, each machine with its own `BSVIBE_MACHINE_ID` |
| **Live UI checks sign in on the disposable stack** (10-19) | `admin@bsvibe.dev` + `~/.bsvibe/e2e-live.password` into `BSVIBE_E2E_PASSWORD` only, never printed, never prod. Export just the 4 public IdP keys from `.env.prod` |

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

## §Ⅱ-c — What 10-15/16 found

* **The repo is PUBLIC.** A security fix's commit/PR text is world-readable ~25 min before the fix deploys —
  write neutral titles, keep repro and internal hosts off GitHub (#1163's first commit was amended before push).
* **`main` requires an up-to-date branch.** Merging the second of two sibling PRs fails until rebased, and
  `gh pr update-branch` is a push (prod action). Rebase locally before the first push.
* **A merge restarts the containers within 2 min**, and intake verdicts in the restart gap may be in neither the
  archived log nor the new container. When a verdict can't be found, keep the filter paused until a quiet point.
* **Inside a workspace scope, layer 2 answers for a deleted predicate** — a wire-cut of a query's own
  `workspace_id ==` stayed green until the test read across tenants (`cross_tenant_read`).
* **The CI runner is this Mac mini**; heavy local test runs slow CI (22 → 30+ min).
* **Auto mode refuses** logging in with another account's stored credentials (even with 형님's go-ahead in chat) —
  rotating a worker of another workspace is 형님's step.

## §Ⅱ-d — What 10-17/19 found

* **"Delivered" was an event, never a fact.** Completion depended on which of "delivery succeeded" and
  "REVIEW_READY" came second. Anything that must hold whatever the order needs a recorded fact plus a sweep.
* **`DockerSandboxSession.exec` dropped `env` silently** — the protocol docstring promised it; the
  verification stack's boot env never reached a server box either (#1174).
* **`approve_run` on a run with nothing pending is a no-op (0, 0)** — there is no "ship without delivering"
  button; a stranded run needs the sweep or a discard.
* **A full-page Playwright screenshot draws fixed elements at the first viewport's bottom** — the "button covers
  the last line" was an artifact; measure on a real scroll.
* **jsdom cannot see layout.** Both settings screens and the tab gap were green in vitest and wrong on screen;
  `apps/pwa/e2e-live/` now has `settings-layout` and `product-detail-and-fab` specs.

## §Ⅱ-e — What 10-19/21 found

* **The worktree was the only thing tying a run to a machine** — and it was worse than "local": before the first
  verify every edit is uncommitted, and `add_run_worktree` silently re-branched from `main` when the path was
  missing. A second machine would have lost the work without an error.
* **MCP work tools run in the API process, not the worker.** With replicas, whichever API answers the call must
  reach the run's tree — hence forwarding, not a shared disk.
* **fakeredis ignores `block` on XREADGROUP** and does not even yield: a consumer loop starves the event loop and
  the test hangs. Sleep on an empty read (#1192).
* **An import contract breaks on the importer's side.** `run_cleanup` importing one constant from `run_checkpoint`
  pulled router/api into the MCP context, which imports `run_cleanup` (#1193, fixed with a domain leaf). Never
  chain `lint-imports | tail -1` in `&&` — the pipe's exit code is `tail`'s.
* **Tests must call worker functions under `workspace_scope(ws)` like prod does** — fail-closed RLS turns a
  missing scope into "no rows", which reads as a logic bug.

## §Ⅲ — Next

0. **10-26 (Mon) 00:30 UTC weekly run** — must end `shipped` with one Telegram message, no human step (#1180).
1. **#929 stage 2** — run artifacts, knowledge vault, skills off local disk. The vault keeps Markdown as the source
   of truth: design first, in `docs/design/stateless-workers.md`. Then stage 3–4 (sandbox concurrency).
2. **#930** — workspace token budget follow-ups (⏳ 형님's ledger-sum SQL first).
3. ⏳ 형님's batch: codex live run (#1183) · `python -m backend.router.accounts.rewrap` dry-run (#1173) ·
   10-26 weekly report · #930 SQL. Walks: #1174 server-sandbox secrets checklist, #1176/#1177 on the next runs.
4. Outside the repo: #937 (cold-boot test, Supabase SMTP). Carry-overs in GitHub issues.

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
* **🐚⭐ zsh does not word-split `$P`** — `P="docker … psql"; $P …` runs nothing ("command not found"). Use a
  function: `P(){ docker --context colima exec -i pg psql "$@"; }`. Happened twice this session.
* **🖥 For a signed-in screen, use the live suite, not jsdom** — answer only the list reads a fresh stack
  cannot (`page.route`), keep the rest real, assert computed styles, look at the screenshots.
