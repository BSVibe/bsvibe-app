# BSVibe session handoff — 2026-10-30

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

## §0 — One line: **Verification now checks what was asked, the way CI checks it — #1255 passed in round 1; real issue runs found and closed nine harness holes**

10-26 → 10-30. Reliability work was done **by hand** (형님 10-26: pushing BSVibe through its own verification gaps was
not converging). Plan R1–R6; R1–R4 shipped, each proven on a real BSVibe run afterwards.

| PR | What | Proven on |
|---|---|---|
| #1245 · #1248 | **R1 requirements ledger** — checkable items extracted from the request (checkboxes, list items under 할 것/가드/완료 기준-style headings); each owed item needs `requirement_evidence` (a changed path, or a test whose file changed / a passing test run covered); per-item judge; PR says `Closes` only when every owed item is `done`, else `Refs` | #1249 → PR #1250 |
| #1251 | **R2** an unchanged tree between failed verifications counts as a repeat (`tree_fingerprint`), stops at `REPEAT_LIMIT` with a `no_progress` question; `runs_detail.verification.failure` shows why | #1204 stall |
| #1252 | **R3** every gate command runs with `CI=true`; product metadata `verify_floor` (every verification) and `verify_final` (once, after everything else passed — the CI-equivalent run); deriver told to keep tests/types/CI steps unnarrowed and on CI's pinned versions | #1204 re-run `d8ae99bd` |
| #1254 | the loop asks once for missing ledger evidence before verifying · the deriver is shown floor/final and does not repeat them · the item judge sees the hunks its item names | #1255 run `cf897406` — **round-1 pass** |
| #1256 | an oversized hunk is shown as a window around the cited test; `requirements_unclear` counted apart from done | — |
| #1258 · #1260 | **R4 counterfactual (report-only)** — each guard item done on cited tests: non-test commits put back to the baseline, the test run alone, `HEAD` restored; row `counterfactual` = holds / vacuous / unknown, ⚠ in the PR table. #1260: it never raises (it once ended a run as `system_error`), and takes away only `baseline..HEAD`, ≤ 200 paths | #1259 run `686d3874` — 4/4 `holds` |
| #1261 | **`verify_precheck`** — the product's quick checks run when the agent says done; a failure is a turn, not a round (≤ 3 per drive) | #1263 run `14e222a1` |
| #1262 | #1259's verified diff, opened by hand — its approved delivery opened no PR (→ #1266) | — |
| #1265 | `.gitignore` `.pnpm-store/` — the sandbox's `pnpm install` put its store at the checkout root and `git add -A` committed 18,745 files onto a run branch; this was the 10-28 `Argument list too long` crash | — |
| #1266 (BSVibe, #1263) | an approved delivery whose action failed or opened no PR is **not** dispatched and raises `delivery_failed`; compensation handles of the actions that did succeed are still stored | PR #1266 opened on approval |
| #1270 | **verification leftovers** — untracked entries that appear during verification go, anchored, into the checkout's `.git/info/exclude` (never deleted, never the repo's `.gitignore`, never the founder's machine); a path the agent writes later is released. Layer 1 of #1267 | — |
| #1271 (BSVibe, #1269) | a run cancelled / discarded under a live drive stops before and after verification, and `write_verified_deliverable` refuses a cancelled run | — |
| #1272 | the item judge reads the **cited test's body** from its file; a cited `file::name` the file does not define fails the item with a reason | — |
| #1273 | **`issues.edited` by the trusted author goes to the unfinished run that issue started** (not a second run); its intent and ledger are refreshed — same item keeps its id, a removed one is `withdrawn` (owed nothing), a new one gets the next id | — |

Issues worked through BSVibe in the same window: #1204 (token rotation, PR #1253), #1255 (run detail shows the checks,
PR #1257), #1259 (runs_list carries a title/preview, not the whole intent; shipped by hand as #1262), #1263 (#1266),
#1269 (#1271). #1244 is the ledger issue itself; #1267 / #1268 were filed record-only and done by hand (#1270, #1273).
Rounds per issue run: #1255 1 · #1259 2 (+1 crash) · #1263 5 (one discard) · #1269 3 — every failed round was a real
defect (type error, broken existing test, a wrong requirement, the 600-line cap, a collection error).

**bsvibe-app product metadata (set 10-27):** `verify_floor` = lint-imports + `tests/architecture` +
`test_h2a_decomposition` + `test_every_decision_has_a_way_out` (~5 s) · `verify_final` = ruff check / format --check,
mypy, full pytest, PWA install+lint+typecheck+test with pnpm 10.33.0 and `--store-dir /tmp/pnpm-store` (~11 min) ·
`verify_precheck` = ruff check, ruff format --check, mypy backend/, lint-imports (~1.5 min, set 10-28).

## §0-b — previous session (10-24 → 10-26): **The merge watch and Safe Mode now agree on what "still being fixed" means; 7 issues fixed through BSVibe, the weekly report is back**

10-24 → 10-26. Every code change was written by BSVibe from an issue filed here, reviewed here (3 denials with a
reason, 2 review comments), approved in Safe Mode after 형님's go-ahead, and merged by the merge watch — except
#1239 (merged by hand, see §Ⅱ-h).

| PR | Issue | What | |
|---|---|---|---|
| #1231 | #1229 | a red CI hand-back carries the Actions job-log excerpt (pytest `FAILED`, ruff/mypy lines; else the tail), fetched only when handing back or giving up; manifest asks `actions: read` | ✅ |
| #1232 | #1230 | a missing App `issues: write` raises one `github_app_permission_missing` report Decision per workspace (the row is the memory, not the process) | ✅ — its review fix was lost, see §Ⅱ-h |
| #1235 | #1233 | report Decisions (`backend/common/decision_kinds.py`) no longer hold a delivered run in `review_ready` | ✅ |
| #1236 | #1234 | the merge watch does not merge while the run is still fixing: run `open`/`running`, or its **latest** deliverable pending in Safe Mode / approved but not delivered | ✅ |
| #1239 | #1238 | a successful delivery moves the Safe Mode item `approved → delivered`; migration `safe_mode_approved_delivered` moves the old ones | ✅ merged by hand |
| #1240 | #1237 | a run re-opened by a review comment that then fails is not merged — `review_comment_unaddressed` Decision whose "fix it again" re-sends the reviewer's words (`pr_comment_record`); a merge that ships a run with a held item records it and tells 형님 | ✅ |
| #1242 | #1241 | the CI-deadline and red-again give-ups wait while the fix is still pending approval | ✅ |

Also: #1201 closed as a duplicate of #1207 (fixed by #1226).

**Operational state, 10-26:**
- **GitHub App Actions: Read-only** granted by 형님 on both installations (10-25). Proven live: PR #1239's red CI went back
  at 16:25Z and the run fixed exactly the two failing tests at the logged lines two minutes later.
- **BStockReport's client path is now `~/Works/BStockReport/main`** (`products.metadata.client_workspace_path`; the old
  `~/Works/BStockReport-client` was gone, last used 10-19). The 10-26 weekly run failed on it at 00:30Z
  (`could not provision this run's worktree:` with an empty detail — a missing cwd prints nothing), was retried at 02:15Z
  and shipped; the report went out through Telegram connector `a5fb238c` at 02:18Z. The `.git` there is a file, so the
  provisioner cannot write `.git/info/exclude` — run worktrees may show as untracked in that checkout; harmless.
- The `product_bundle_publish_skipped_shallow` warning stopped after that run (0 in the first 400 s of the 03:35Z container).
- `safe_mode_approved_delivered` ran on deploy (10-26 02:14Z). How many rows it moved is not logged — unknown.

## §Ⅰ — Decisions still in force (형님, 2026-10-01 · 10-04 · 10-16 · 10-19 · 10-20 · 10-21 · 10-23 · 10-26 · 10-28 · 10-29 · 10-30)

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
| **BStockReport runs from `~/Works/BStockReport/main`** (10-26) | The bare-repo worktree replaces the deleted `BStockReport-client`; the run worktrees are made inside it |
| **Reliability work by hand** (10-26) | The verification harness (R1–R6) is written here, not through BSVibe — an exception to "BSVibe fixes BSVibe" while the harness is what is being fixed. Product issues still go through BSVibe |
| **The goal is a round-1 pass** (10-26) | "실제 프로덕션까지 가려면 1라운드만에 요청한게 통과해야 해" — judged on real issue runs, stack- and deliverable-agnostic (no Python-only fix) |
| **R4 is report-only** (10-28) | A guard test that passes with the change taken away is marked, never failed — regression tests of existing behaviour pass legitimately. Gate it only after its false-positive rate is measured (R6) |
| **Harness issues are filed record-only, fixed by hand** (10-29) | A verification-harness defect found on a run goes up with the binding filter paused and is implemented here; a product defect goes up as a normal issue for BSVibe |
| **Leftovers are excluded, not deleted** (10-30) | What verification leaves in a checkout goes into that checkout's `.git/info/exclude`; the model-derived layer 2 of #1267 waits until layer 1's logs have been seen |

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

## §Ⅱ-h — What 10-24/26 found

* **A Safe Mode deny with a reason re-drives the SAME run**; it does not cancel it. A comment on the issue after a deny
  started a duplicate run (`01b6dd55`, cancelled). Deny with `rejected_approach` + the correction, and nothing else.
  `queue_cleanup` is the one that releases the run.
* **The merge watch merged a reviewed head while the fix waited for approval** (#1232, 10-25 04:54Z): CI went green on
  the old head, the watch merged, the run shipped and its pending fix vanished from the queue (the pending list hides
  items of shipped runs). → #1234, #1237.
* **Nothing ever moved a Safe Mode item past `approved`** — `SafeModeQueueService.mark_delivered` had no caller (the
  `relay_worker` one is the audit outbox). Two of #1234's three attempts were wrong because of it: counting `APPROVED`
  as "waiting" would have stalled every merge for good; counting any undelivered deliverable would have let a run's
  own denied attempt block it forever (a deny does not set `retracted_at`). Judge by the latest deliverable. → #1238.
* **The CI deadline counted the founder's approval time** — #1239's fix waited overnight for approval and the watch gave
  up at its one-hour deadline, so the green PR was merged by hand. → #1241.
* **A run checks only the tests it chose.** #1239 went red on two older tests that pinned the old behaviour; #1240 on a
  `SimpleNamespace` stub. CI + the job-log hand-back fixed both without a human.
* **Filing a follow-up before its base merges makes the run stop and ask** (#1237 asked "#1234 is not on main"). File
  dependent issues after the base PR merges.
* **Two PRs adding a same-named test helper conflict** (`_stalling_worker`, #1240/#1242); the merge watch sent the
  conflict back and the run renamed one. Expected, not a defect.
* **A missing cwd makes the host executor's exec fail with no output** — "could not provision this run's worktree: " with
  nothing after the colon means look at the path first.

## §Ⅱ-i — What 10-26/30 found

* **Verification passed what was not asked and failed on what no work could fix.** #1204's first run did one of five
  items and passed; its re-run failed 8 rounds on the sandbox lacking `CI=true` (pnpm aborts without a TTY). Both were
  harness facts, not agent quality.
* **The model re-derives the gate each round**, so failure *text* drifts and "same failure 3×" never fired. The tree is
  what cannot drift (R2).
* **A gate command loop existed twice** (server and in-place gate) and they had drifted; one runner now
  (`gate_commands.run_gate_commands`).
* **Telling the deriver "don't repeat the product's checks" moved type errors to the final check** — #1259 spent a
  round on a one-line mypy error. `verify_precheck` is the answer (this PR).
* **A report-only check ended a run.** R4 passed a path list from the working tree to one `git` argv; round 2's list
  was too long (`Errno 7`) and the exception was not caught. Where the long list came from is **still unknown** — the
  next `counterfactual_skipped` log line carries the count.
* **The ledger judge read the head of long diffs**, then nothing at all for a single 5,900-char hunk — "unclear" items
  turned `Closes` into `Refs` (#1204, #1255 had to be closed by hand).
* **Intake logged nothing for ~14 h** (10-28 10:41Z deploy → 10-29 01:10Z, 43 webhooks `202`, no intake verdict,
  no worker error). After `docker restart bsvibe-prod-worker-1` a probe comment was `filter_rejected` at once. Cause
  unknown — if a run does not start after an issue, check `intake_worker_*` in the worker log first.
* **An approved delivery can open no PR and still count as dispatched** (#1259 run `686d3874`: `dispatched_count: 1`,
  no PR, no branch, no `delivery_failed`, compensation handles stored). Shipped by hand as #1262; tracked as an issue.
* **The auto-mode classifier blocks `gh pr merge` on a bare "응" and issue writes it cannot tie to an explicit ask.**
  Ask 형님 for an explicit "N 머지해" / "닫아".

* **The 10-28 "Argument list too long" crash was the pnpm store.** `pnpm install` in the sandbox puts its store at the
  mount root, inside the checkout; the next round's `git add -A` committed it (18,745 files). A tool's store location
  depends on the machine, not the stack — that is why #1270 excludes by observation, not by tool name.
* **Discarding a run did not stop its live drive** — a minute after `runs_discard` the run verified and made a new
  deliverable and Safe Mode card (fixed by #1271). Clear such a card with `queue_cleanup`.
* **Every "unclear" in the ledger had a mechanical cause**: a test body past the excerpt budget, or a cited test name
  that does not exist (a file's name, a never-written test). #1272 reads the body and fails missing names.
* **An issue edit started a second run for the same issue** (fixed by #1273). Before #1273, editing an issue with a
  live run needed the filter paused.
* **A plain "응" does not clear the auto-mode classifier for `gh pr merge` / issue writes**; "N 머지해" / "N 닫아" does.

## §Ⅲ — Next

1. **Run more real issues** and record rounds per run — the round-1 pass rate is the measure (R6 makes it a metric).
2. **R5 independent reviewer**, **R6 metrics** (round-1 pass rate, rounds per run, unclear / vacuous rates) — open.
3. **#1267 layer 2** (the deriver names the stack's cache paths up front) — only after `verification_leftovers_excluded`
   logs have been seen on real runs.
4. **#929 turn-on** — a `BSVIBE_MACHINE_ID` on this box alone, then a second machine
   (`docs/runbook/vault-to-postgres.md` §"A second box"). `/app/var/vault` may go now (kept until 10-29).
5. **#930** — 2M tuning still has no sample.
6. ⏳ 형님's batch: codex live run (#1183) · #1046 live taps · #937 outside the repo.
7. E2E checklists still open: `docs/e2e/requirements-ledger-checklist.md`, `ci-parity-checklist.md` (B failing-final,
   D, E, F), `counterfactual-guard-checklist.md` (B, C).

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
