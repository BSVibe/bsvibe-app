# BSVibe session handoff — 2026-10-23

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

## §0 — One line: **BSVibe now fixes BSVibe — issues, review comments and red CI go back to the run that owns the PR; 15 harness/delivery defects found and fixed through that loop**

10-22 → 10-23. Every code change below was written by BSVibe itself from a GitHub issue or a PR comment,
reviewed here, approved in Safe Mode, and merged by its own merge watch — except where noted.

| PR | Issue | What | |
|---|---|---|---|
| #1206 | #1202 | worker re-register: 409 instead of 500 on a live same-name worker; `register` defaults to the login issuer | ✅ |
| #1210 | #1209 | the delivery sweep no longer re-"ships" a run awaiting merge every 5 s | ✅ |
| #1211 | #1208 | a re-opened run: baseline = branch point; `NO_FAILURE_FOUND` / 3× same failure → Decision; "zero gate commands" is reported; founder answers reach the gate deriver | ✅ |
| #1213 | #1212 | the baseline is re-read every round and picks the newer of `main` / `origin/main` | ✅ |
| #1214 | #1203 | `python -m backend.identity.revoke_access_tokens` + OAuth key rotation runbook §3b | ✅ |
| #1217 | #1216 | verification and the deliverable diff share one branch point (`backend/storage/branch_point.py`) | ✅ |
| #1218 | #1215 | a failed GitHub action = not delivered, not shipped, one `delivery_failed` Decision; run branches push `--force-with-lease` | ✅ merged by hand (see §Ⅱ-g) |
| #1219 | (#1218 review) | `delivery_failed` gets a question and working "send again / discard" actions | ✅ |
| #1222 | #1221 | the GitHub App manifest asks `issues: write` (+ runbook `github-app-permissions.md`) | ✅ merged by hand |
| #1223 | #1220 | a write-access reviewer's comment on a BSVibe PR goes to the run that opened it; everyone else's is a new request | ✅ merged by hand after a 600-LOC split |
| #1225 | — | **hotfix:** a nested `workspace_session_scope` restores the outer tenant (intake outage 09:21–09:22Z) | ✅ written here |
| #1226 | #1207 | KMS `PREVIOUS_KEYS` reaches the containers; rewrap runbook command | ✅ |
| #1227 | #1224 | merge watch: per-head CI deadline, red CI sent back to the run (×2), "fix it again", watches a given-up PR until it merges/closes | ✅ merged by hand; its 400-LOC fix came through a PR comment routed to the same run (#1223) and was force-with-lease pushed to the same PR (#1218) |

Also: #1204 (token connectors cannot rotate their token — open), #1205 (vault cut-over record, by hand).

**Operational state, 10-23:**
- **Vault + skills are rows** since 10-22 03:29Z; positive proof seen 10-22 08:19Z (a new `seeds/request/…` landed as a
  `vault_documents` row, 0 files written under `/app/var/vault` since the cut-over). Keep the directories until 10-29.
- **Telegram** moved to connector `a5fb238c` (10-22): chat_id + `authorized_user_ids` re-set, both bindings re-created,
  webhook re-registered. First real send is the 10-26 report.
- **GitHub App `bsvibe` now has Issues: write** — granted on BOTH installations (`blas1n` and the `BSVibe` org; each
  installation approves separately). Probe: an empty-body `POST …/issues/<n>/comments` → 422.
- **Credentials:** `rewrap` dry-run `current 26 · would rewrap 0 · undecryptable 0`.
- Runs `7709a7a2`, `6fa72ec1`, `e2756fcd` had sat at `review_ready` after their PRs merged (the watch had given up);
  they moved to `shipped` at 11:46Z, right after #1227 deployed (migration `merge_watch_ci_fix` ran).

## §0-b — previous session (10-21 → 10-22): **#929's code is done — the vault and skills can live in Postgres, and a second machine no longer loses work; turning it on is 형님's step**

10-21 → 10-22. All five deployed (`bsvibe-app.deployed` = `0e03c308`), host worker restarted 10-21 16:25 KST.
**The vault and skills now live in Postgres** (cut-over 10-22 03:29Z — §Ⅲ 1).

| PR | What | Prod check |
|---|---|---|
| #1195 | `vault_documents` (RLS) + `PgDocumentStorage`, the second `StorageBackend`, behind shared contract tests | ✅ backend booted on `alembic upgrade head` 10-21 07:27Z · live since the 10-22 cut-over |
| #1196 | `Vault` I/O and the garden writer go through the store; one factory `backend.knowledge.vault_store` + `BSVIBE_KNOWLEDGE_STORE_BACKEND` | ✅ live since 10-22 |
| #1197 | originals, indexes, code graph, MCP, `/inside` and skills through the store; AST guard on direct file I/O; `python -m backend.knowledge.store import [--dry-run] \| export <dir>`; compose passes `BSVIBE_MACHINE_ID` + the store backend (they were never reaching a container) | ✅ store live 10-22, machine id unset · runbook `docs/runbook/vault-to-postgres.md` |
| #1198 | bootstrap clones full history (a shallow repo cannot be bundled); a deliverable's file is read from the run's checkpoint when the tree is on another machine; vault locks are real (`StorageBackend.lock`: advisory lock on rows, `flock` on files) | ✅ |
| #1199 | stage 4: A→B→A restores the newer checkpoint · a tool call for a run nobody drives is refused when routing is on · the sandbox reaper actually runs (API process) · verification compose projects carry the machine id | ✅ design doc §6 |

**#929 stages 1, 2 and 4 are merged; stage 3 did not exist.** Stage 1 is still inert (no `BSVIBE_MACHINE_ID`). — §Ⅲ.

**#1046 was already implemented** by #1047 (09-23). Checked 10-22 over MCP: the prod Telegram account
carries `authorized_user_ids: ["8242700007"]` with `chat_id` unchanged, so the migration ran and seeded the
allowlist. The live taps remain (1:1, group, a refused tapper) (`docs/e2e/telegram-approval-allowlist-checklist.md` §B).

## §Ⅰ — Decisions still in force (형님, 2026-10-01 · 10-04 · 10-16 · 10-19 · 10-20 · 10-21 · 10-23)

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
| **The vault and skills move into Postgres rows, Markdown unchanged** (10-21) | #929 stage 2: one `vault_documents` table behind `StorageBackend`, import/export to a directory. Not R2 objects, not a shared mount — `docs/design/stateless-workers.md` §5 |
| **Live UI checks sign in on the disposable stack** (10-19) | `admin@bsvibe.dev` + `~/.bsvibe/e2e-live.password` into `BSVIBE_E2E_PASSWORD` only, never printed, never prod. Export just the 4 public IdP keys from `.env.prod` |
| **Anyone's issue or comment is work — by design** (10-23) | A real AI-agent company answers an incoming issue at once; the front triage drops the noise. Do not gate intake by author. Only a path that writes straight into a running run (a PR review comment → its run, #1223) requires OWNER / MEMBER / COLLABORATOR; anyone else's comment is a normal new request |
| **BSVibe fixes BSVibe** (10-23) | Code changes go through BSVibe: an issue, or a comment on its PR. Review the deliverable, deny with a reason (`rejected_approach`) or approve; BSVibe's merge watch merges. Hand-written code only to unblock that loop itself (#1223's split, #1225's hotfix) |
| **A PR review comment fixes THAT PR** (10-23) | It goes to the run that opened the PR (same branch, force-with-lease), never a second PR off main (#1220/#1223) |

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

## §Ⅱ-f — What 10-21/22 found

* **A lock made per instance locks nothing.** The garden writer and canonicalization each built an
  `asyncio.Lock` per instance, and instances are made per request — two requests, or the API and the worker,
  never excluded each other. On rows an append is a read plus a write, so it would have been a lost update (#1198).
* **A background loop documented as started can be started nowhere.** `sandbox_reaper_loop`'s docstring said
  the API lifespan started it; nothing did. One box masked it (the worker re-adopted the box on resume) (#1199).
* **A setting the compose file does not pass is a setting prod never sees.** `BSVIBE_MACHINE_ID` had been
  missing from `compose.prod.yaml` since #1189 — stage 1 would have stayed off on every box (#1197, deploy test pins it).
* **The runbooks' `python -m …` never worked in the image** — its Python is `/app/.venv/bin/python`; a bare
  `python` has no dependencies. And `docker compose -p bsvibe-prod run` needs the project files (fixed 10-22).
* **A pause window with no webhook proves nothing.** Check that deliveries actually arrive before reading
  "no verdict" as "rejected".
* **A token connector cannot rotate its token** — replacing the connector strands its bindings and empties
  its `delivery_config` (10-22: Telegram moved to `a5fb238c`; chat_id + allowlist re-set, both bindings
  re-created, webhook re-registered). Issue filed the same day.
* **A `--depth=1` clone cannot be bundled**, so a product bootstrapped shallow could not be restored on another
  machine (#1198).

## §Ⅱ-g — What 10-22/23 found

* **Merges by BSVibe show as `blas1n`.** The GitHub App acts with a user-to-server token (`ghu_`), so "merged by blas1n"
  is usually the merge watch, not 형님 — read `github_merge_watch.status` before telling 형님 to merge.
* **A comment on a BSVibe PR used to fork a second PR, and its merge closed the first** — BSVibe closes the
  originating "issue" after a merge, and the originating issue of a PR comment is that PR. #1218 was closed unmerged
  this way and recovered by hand (#1220 → #1223 fixed the routing).
* **A nested `workspace_session_scope` stripped the outer tenant** → `INSERT INTO requests` failed RLS on every
  intake iteration (10-23 09:21Z). Pausing the binding filter drained the poisoned trigger in one tick (the filter
  runs before routing); #1225 fixed the scope. SQLite tests cannot see this — a PG test now pins it.
* **A run's own verification only runs the tests it chose.** Two PRs went red in CI on repo-wide guards the run never
  ran (`test_every_decision_has_a_way_out`, `test_h2a_decomposition` 600 LOC). Read CI, not only the deliverable.
* **The merge watch gives up on PRs it touched itself** (deadline not restarted after its own freshen; red CI had no
  way back to the run; a given-up PR's later merge was never noticed) → #1224/#1227.
* **A re-opened run measured its changes from the wrong point** — its own first attempt vanished, then main's commits
  leaked in after a mid-drive rebase, then a stale local `main` did the same → #1211/#1213/#1217. A run that could not
  pass looped 30 rounds (~960k tokens) on `NO_FAILURE_FOUND`; Claude Code followed the harness's instruction exactly.
* **The App manifest never asked `issues: write`**, so the "PR opened" comment on the issue had always 403'd.

## §Ⅲ — Next

0. **10-26 (Mon) 00:30 UTC weekly run** — must end `shipped` with one Telegram message, no human step (#1180), and is the
   first send through the new Telegram connector `a5fb238c`. After it, BStockReport's
   `product_bundle_publish_skipped_shallow` warning (every 5 min) should stop — if not, that product cannot reach the
   bundle store.
1. **#929 turn-on** — the vault cut-over is done and proven (§0). Next: a `BSVIBE_MACHINE_ID` on this box alone, then
   a second machine (`docs/runbook/vault-to-postgres.md` §"A second box"). Keep `/app/var/vault` until 10-29.
2. **Open follow-ups:** #1204 (token connectors cannot rotate a token in place) · the `merge_watch` CI hand-back sends the
   check's name but GitHub Actions' `output.summary` is usually empty (the run re-runs the tests itself) · #1221 only
   logs a missing App permission — tell 형님 instead.
3. **#930** — 2M tuning still has no sample (2 shipped runs since 10-01).
4. ⏳ 형님's batch: codex live run (#1183) · #1046 live taps · #937 outside the repo.

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
* **🐳 The default docker context is now prod (`colima`) — `colima-palworld` is gone (seen 10-23).** Always pass
  `--context colima` explicitly, never `docker context use`, and do not start throwaway containers on this box: there
  is no non-prod docker here any more. Verify on Postgres through CI.
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
