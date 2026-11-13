# BSVibe session handoff — 2026-11-13

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

## §0 — One line: **The re-audit's five cross-tenant security findings are fixed and deployed; BSVibe is still not ready for strangers**

11-11 → 11-13. Every card below was reviewed (diff read, review findings checked against the code) before it was
approved or denied. "Rounds" is `passed_on_round` / all verification rounds of the run, across denials.

| Issue → PR | What | Rounds |
|---|---|---|
| #1364 → #1365 | **monthly token budget is unlimited by default** (tokens are BYO); owner/admin may set a spend limit. Per-run cap (2M) unchanged | 3 / 3 |
| — → #1366 | public re-audit `docs/audit/multiuser-readiness-2026-11-12.md` (security details withheld; full report kept off GitHub) | — |
| #1367 → #1372 | GitHub App setup flow is operator-only | 1 / 3 (two denials) |
| #1368 → #1373 | repository bindings and webhook routing are tied to repository access | 2 / 12 (a denial; then the run hit the 2M cap on automatic merge-conflict re-dispatches → #1387) |
| #1370 → #1374 | connector API URLs pass the egress check | 1 / 1 |
| #1371 → #1377 | the sandbox firewall rules also cover user-defined networks | 1 / 3 |
| #1375 → #1378 | **ledger verdicts stop flipping between rounds**: "excerpt not shown" is `cannot_determine` → `unclear` (passes), verdicts kept per file-content key | 2 / 3 |
| #1376 → #1379 | users can no longer set a GitHub connector's `external_ref`; a cleanup migration cleared unverified ones | 2 / 2 |
| by hand → #1383 | **intake outage fix**: an App delivery for an account with no recorded installation asks the App (`GET /repos/{repo}/installation`, App JWT) and records it when it matches | — |
| #1369 → (denied, cancelled) → #1380 → #1385 | **Obsidian import is confined to a server-side root** (`<import_root>/<workspace_id>`, fail-closed when unset), symlinks never followed, note / entry / byte caps during the walk | 2 / 2 |
| #1381 → #1384 | a founder's free-text checkpoint answer is recorded as a ledger correction too | 1 / 1 |
| #1382 | closed unbuilt — its premise was wrong: `claim_install` already records the installation id (since the 09-10 audit) | — |
| #1386 → #1388 | **pending decisions close when their run ends** (`run_reached_terminal`, run untouched; `delivery_failed` / `merge_watch_stalled` kept); `discard`/`retry` on an ended run closes only the decision; a migration swept the backlog — **checked live 11-13: the pending-checkpoint queue went from 3 to 0** | approved after one denial |
| #1387 → #1389 (open) | **merge-conflict re-dispatch has run-wide caps**: 4 re-dispatches in total (a new head no longer resets the count), 500k tokens after the first conflict (never above the run cap); hitting either goes straight to `merge_conflict_review` with what was tried; an in-turn hit marks the budget `exhausted` so the stalled-head path cannot re-open the run | approved after two denials |

**Founder corrections are proven live**: #1386's re-work card showed R3 under `ledger_corrected` — the item came back
done on the founder's denial reason, not the issue's sentence.

**Security status**: the three blocker and two high cross-tenant findings of the 11-12 re-audit (#1367–#1371) are
merged and deployed, plus the two follow-ups they exposed (#1376, #1380). The audit's other blockers are not security
and are not filed yet (§Ⅲ).

## §0-b — previous session (11-04 → 11-11): **A worker restart mid-run is now proven safe in prod; a founder's denial reason now corrects the ledger**

11-04 → 11-11. Decision 11-04: HANDOFF §Ⅲ items go up as issues for BSVibe. Each run below was reviewed before its
Safe Mode card was approved; one was denied with a reason and re-driven.

| Issue → PR | What | Rounds |
|---|---|---|
| #1314 → #1315 | **the agent worker drives runs concurrently** (`agent_drive_concurrency`, 0 = `sandbox_max_concurrent`) — proven live 11-05: #1316 and #1317 claimed at the same second and driven side by side | 2 |
| #1309 → #1312 | `leftovers_excluded` stored on the verification result — **proven in the DB** on #1316's run (`{"count": 1, "dirs": [{"path": "var"}]}`) | 1 |
| #1317 → #1318 | `verification_quality` counts `rounds_with_leftovers` and the top `leftover_dirs`; `verification_feedback._failed` is public (`failed_commands`) | **1** |
| #1316 → #1319 | the PWA run detail shows the files verification kept out of the commit | 2 |
| #1320 → #1321 | **a ledger item that asks for commands to pass is decided by running them** — the deriver returns the repo's runnable form tagged `covers` (`cd apps/pwa && …`); the request's string is never run as written; an unplaceable command is not guessed | 1 (denied once: it ran the string at the repo root, where the PWA commands always fail) |
| #1322 → #1324 | no runnable form → the judge's verdict stands (with a note); `covers` honoured only when the form carries the command's words | 4 (one orphaned by a deploy, retried) |
| #1325 → #1326 | `covers` must sit in the command position, and a form that hides the exit code (`\|\| true`, `; true`, a pipe, `&& true`) is refused | **1** |
| #1327 → #1328 | **the ledger keeps a checklist item's sub-list** (`head — sub1; sub2`, 800 chars, `…` when cut); a sub-checkbox is no longer an item of its own | **1** |
| #1330 → #1331 | **R5 independent review (report-only)** — every PASSED round (after the final checks) gets one fresh-context review: request + ledger + whole `baseline..HEAD` diff + where non-test code calls each new public symbol ("운영 호출 없음") + the gate's commands; stored as `result["review"]` (`done` + findings `{severity, file, line, claim, failure_scenario}` / `unavailable`); the same tree is not reviewed twice | 1 (+ a CI flake the run fixed itself) |
| #1332 → #1333 | the review shows on the Safe Mode card (`review_status` / `review_counts` / top 5), in the PR body ("## Independent review" after the ledger) and in `verification_quality` (`reviews_done` / `_unavailable`, `review_findings_by_severity`, `runs_reviewed_with_high`) — **live from #1334's card and PR #1335** | 2 |
| #1334 → #1335 | the review's own first two findings: PR-body lines made one inert line; `runs_reviewed_with_high` judged on the last passed round, like the card | **1** |
| #1336 → #1337 | the review's next finding: `file` / `line` validated when stored, the location flattened in the PR body | 2 |
| #1339 → #1340 | **a non-code artifact with no commits is a clean skip, not `delivery_failed`** — #1266 had made every "no PR opened" a failure, so the weekly BStockReport (`direct_output`) raised a spurious decision on 11-02 and 11-09 although the report reached Telegram; both decisions discarded | 4 (three lost to a GitHub Releases 504 in the parser download → #1341) |
| #1341 → #1348 | **tree-sitter parsers prefetched at image build** (backend + sandbox, `/opt/tree-sitter-parsers`, backend `BSVIBE_PARSER_OFFLINE=1`), version pinned, CI cache — runtime never downloads. Delivered **by hand**: the GitHub App lacked `workflows` permission (now granted) | 1 (+ a denial led by the review) |
| #1343 → #1344 | **a delivery that fails right after a Safe Mode approval raises `delivery_failed`** — the decision factory was wired only in the worker process, so approvals (backend: REST / MCP / Telegram) logged `delivery_failure_decision_unwired` and left the run stuck with nothing to resend | 4 (three lost to the gate's duplicate coverage run timing out → #1346) |
| #1345 → #1349 | **checkpoints work for a GitHub run's own `--depth 1` clone** (#929): thin bundle with no network/auth at save; restore = clone like the provisioner (credential, token-free origin) + fetch the checkpoint; the branch is rebuilt from the snapshot's parent and its recorded name | 1 (+ two denials, both led by the review: no `origin` after restore; an uncommitted run's branch dropped from the bundle) |
| #1346 → #1347 | the derived gate drops a whole-suite `pytest` that only re-flags the product's own final run, and a timed-out command is listed apart instead of "re-emit verbatim" | **1** |
| #1350 → #1352 | the "send again" test checks that exactly one new delivery event (founder-approved, same run) is added, and that resolving the decision twice adds none | **1** |
| #1353 → #1354 | `covers_rejection` refuses `\|\|` / `;` / `\|` anywhere after the ledger command and a lone background `&`; an env prefix (`CI=true …`) is skipped like a launcher | **1** |
| #1356 → #1357 | **a turn that outlives its job is cut off**: the run-scoped task token carries the run's claim and the work tools refuse it once the run is no longer RUNNING under that claim (`mcp_work_claim_lost`); a cancelled job kills its executor session (`cancel_task`); a sandbox name conflict is cleared and retried once | **1** |
| #1355 → #1358 | `duplicates_product_run` knows the pytest-cov value flags; `--cov` takes the next word only when it is not an option or a test path | 3 (one forced fail by a worker restart before #1357; one denial led by two high review findings — the issue's own wording was wrong; one round lost to the ledger holding that wording → #1359) |
| #1359 → #1360 | **a founder's `rejected_approach` reason corrects the ledger**: recorded on the run (`founder_corrections`, founder-typed text only, numbered), given to the item judge as FOUNDER CORRECTIONS that outrank the request (later supersedes earlier); an item done by a correction is marked (`founder_correction`, PR table "✎ 창업자 정정 반영", card `ledger_corrected`) | 1 (+ a denial: the card had no mark, corrections had no order) |
| #1361 → #1362 | the PWA card lists `ledger_corrected` items as info, not an alert | **1** |
| by hand → #1323 | **a worker restart gives its drives back**: `stop()` cancels in-flight drives (their cancel path releases the claim), then releases whatever it still holds; on start a worker takes back the claims a previous process left on its own machine | — |

Rounds per issue run this session: #1314 2 · #1316 2 · #1317 **1** · #1320 1 (+ a denial) · #1322 4 · #1325 **1** ·
#1327 **1** · #1330 **1** · #1332 2 · #1334 **1** · #1336 2. #1332's and #1336's first rounds failed on the full
`pytest -q` final check (an existing test the change broke) — not on the ledger.

**The independent review's sample (11-06 → 11-10): every finding so far was real.** The first three (low) were
fixed by the next runs. On 11-10 it caught what the founder's own review would have: the #1341 test re-introduced the
network dependency it was meant to remove (3 medium), and #1345's restore had no `origin` (high) and dropped an
uncommitted run's branch (high) — both cards were denied with the review's findings as the reason, and the re-work
passed. Earlier sample: markdown injection into the PR
body from reviewer text (#1333), a metric on a different basis than the card (#1333), an unflattened `file` field
(#1335). None was a false alarm; two were fixed by the next run.

## §Ⅰ — Decisions still in force (형님, 2026-10-01 · 10-04 · 10-16 · 10-19 · 10-20 · 10-21 · 10-23 · 10-26 · 10-28 · 10-29 · 10-30 · 11-03 · 11-11)

| | |
|---|---|
| **Open-ended free beta** (10-16) | Billing is **paused** (business registration). Everyone is on Free: 3 concurrent runs · 1 verification stack · no monthly token limit by default (11-11). No trial period, no expiry. Nothing in the UI may say "upgrade" |
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
| **The run payload has one writer: the drive** (11-03) | Anything sent to a running run (issue edit, issue comment, PR comment) goes to its inbox (`run_inbox.post_to_inbox`); never write `run.payload` from intake or any path that runs beside a live drive |
| **Harness items run through BSVibe too** (11-04) | "handoff 내용들을 bsvibe로 돌려서 표본을 늘려보자" — the HANDOFF §Ⅲ items go up as issues for BSVibe, which also grows the round-1 sample. Supersedes "Reliability work by hand" (10-26) for new items; a harness hole a run exposes may still be fixed by hand when it blocks the loop (#1311) |
| **Leftovers are excluded, not deleted** (10-30) | What verification leaves in a checkout goes into that checkout's `.git/info/exclude`; the model-derived layer 2 of #1267 waits until layer 1's logs have been seen |
| **No platform token limit** (11-11) | "우리는 서버에서 ai 돌려주는게 아니라, 사용자 ai를 쓰는거라 토큰 상한을 걸 이유가 없지 않나?" — tokens are BYO, so the monthly budget is unlimited by default and only a spend limit the owner chooses (#1365). The per-run cap (2M) stays as a runaway guard; #930 closed |

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

## §Ⅱ-j — What 10-30/11-04 found

* **The issue text was wrong, twice, not the agent.** #1279 asked to count `artifact_refs`, which lists only files
  written through the work tools (run 14e222a1: 7 refs, 18,752 files in the PR). Check what a field really holds
  before writing it into an issue — the run implements the issue exactly.
* **A private org member is `CONTRIBUTOR` in GitHub's webhooks.** Every trust check on `author_association` refused
  the founder: issue edits (#1281), PR comments (#1281), checklist comments in the ledger (#1286).
* **Intake wrote `run.payload` while the drive held its own copy** — two routed edits on #1293 were written over,
  one by framing's copy, one by the drive's; the drive also read routed edits only at drive start. The inbox (#1294)
  makes the drive the payload's only writer.
* **An executor works in ONE long turn.** A per-turn hook never fires inside it; a comment that lands during it was
  never told — the turn ended "done" and verification went straight on (#1296). The "done" branch reads the inbox
  first (#1298).
* **Every remaining "unclear" had a mechanical cause again**: several cited paths sharing one head-cut budget
  (#1283), cited code the run did not change (#1285), helpers and fixtures defined above the cited test (#1289), a
  wrapped test signature read as the end of the test (#1292), helpers imported from a sibling test module (#1304).
* **Re-opening a run did not clear its pending Safe Mode card** — an old card stayed approvable after the founder's
  message re-opened the run (fixed by #1303: the re-open denies it with a "superseded" reason).
* **A comment on an issue with a live run used to queue a second run** (#1279's `b9d0039c`, discarded by hand) —
  fixed by #1294.
* **The agent worker drives runs one at a time.** `AgentWorker.drive_once` walks its batch serially; "3 concurrent
  runs" is a cap on open runs, not parallel execution — #1309's run waited 23 min for #1308's to finish (11-04).
* **Every ledger-only round had a cause the loop could read first**: an unchanged cited path (#1046 re-run → #1305),
  a cited test name the file did not define (#1308 → #1311).
* **I picked an issue that was already done (#1046 → #1047).** Check `git log -S` / the code on main before filing
  or re-running an issue.

## §Ⅱ-k — What 11-04/11-11 found

* **A deploy stranded the runs it interrupted for two hours.** `AgentWorker.stop()` waited for its drives; a drive can
  be an hour into one executor turn, so the container was killed first and the claim stayed until the stale-claim
  lease (2 × `executor_task_timeout_s`). #1322's run sat RUNNING with nobody driving it (11-05). With concurrent drives
  and the merge watch's autodeploys this would hit often — fixed by hand (#1323). After #1323 the worker logs
  `worker_runtime_stopped` on a deploy; **a deploy during a live drive has not been seen yet** (checklist below).
* **The gate never ran the commands an issue said must pass** — the deriver was told to leave ledger items alone, and
  the judge only reads cited evidence (#1316's R5: `rejected`, then `unclear`; only the PR's CI said whether lint /
  tsc / vitest passed). Running the issue's string as written is wrong too: the PWA's commands fail at the repo root.
* **The ledger dropped every sub-list under a checklist item** (#1325: R4 read only "테스트:"; the judge could not
  judge it). Issues written with nested bullets lost their substance; fixed by #1328.
* **An issue requirement that asks for evidence a diff cannot carry stalls the ledger.** "Cut the wire and show the
  test go red" (#1322 R4, #1325 R5) stayed missing / `unclear` — the run has no way to record that it did it. Put
  wire-cut proof in your own review, not in an issue requirement.
* **I misread `gh issue create … &` output.** Two issues filed in parallel print their URLs in finish order; I paired
  #1316/#1317 with the wrong bodies and nearly "fixed" BSVibe's correct `Closes`. Read the title back.
* **A test that seeds rows in a workspace scope must commit inside it** — on Postgres the RLS GUC is gone once the
  scope exits (#1323's first CI run). SQLite does not catch it; check on a throwaway Postgres.

* **The review's first findings were things the ledger judge cannot see** — they are about the whole change (an
  unescaped field reaching the PR body, two views counting on different bases), not about any one item.
* **Autodeploy already defers while an executor run is alive** (`_infra/scripts/autodeploy.sh`, `run_liveness.sh`);
  #1322's run was orphaned in the gap right after its claim, before it counted as alive. #1323 covers that gap and a
  forced kill; a mid-drive deploy is therefore rare and has not been seen since.
* **Ledger commands are proven live**: #1332's R6 ("`pnpm lint`·`tsc`·`test` 통과") was decided by the gate running
  `cd apps/pwa && …` forms tagged `covers` — `done` with "명령 통과", no evidence citing needed.

* **#929 stage 1 is on** (11-09): `BSVIBE_MACHINE_ID=mac-mini-1` in `deploy/.env.prod` (backup `.env.prod.bak-20261109`),
  applied by the next autodeploy. It switched on checkpoints, which immediately showed GitHub clone runs were never
  covered (`run_checkpoint_failed`, fixed by #1349). Watch for `run_worktree_checkpointed` on the next released run.
* **A test that downloads at runtime turns a GitHub outage into failed rounds** — three of #1339's rounds and #1342's
  CI were lost to `tree-sitter-language-pack` fetching parsers (504). #1348 prefetches at build time.
* **The gate re-ran the product's own full suite with `--cov -v`** and re-emitted it after it timed out — three of
  #1343's rounds lost (#1347).
* **An approval-path delivery failure was silent** (#1343): no decision, card consumed, run stuck `review_ready`.
  Wiring that only one process does is a class of bug — check both backend and worker when a factory is injected.
* **The GitHub App had no `workflows` permission**: an approved change to `.github/workflows/` cannot be pushed by
  BSVibe until it is granted (granted 11-10; the app is owned by the founder's account, installed on the org).

* **#1323 is proven live (11-11)** — by restarting the prod worker by hand mid-turn (founder's OK), graceful and
  `docker kill`; see the checklist. The first attempt (11-10, before #1357) **killed the run**: `stop()` released it,
  but the executor turn runs outside the worker and kept calling the work tools; the backend created the run's
  sandbox for that stale call and the re-drive failed on `container name … already in use` → #1356.
* **Restarting a worker is the way to exercise drive-lifecycle code** — autodeploy defers while a run is alive, so a
  mid-drive deploy almost never happens on its own. Wait until the run is calling tools (backend `mcp_work_tool`),
  then restart; read worker AND backend logs, and check the run status afterwards — "re-claimed in 1 s" said nothing
  about whether the re-drive survived.
* **A denial reason that corrects the request was invisible to the ledger** (#1355): the judge compared the fixed code
  with the issue's wrong sentence and rejected it; the agent escaped by marking the item `deferred` while it was
  done. #1359/#1360 lets the founder's own reason outrank the request.
* **My issue wording was the defect twice** (#1353 claimed `CI=true …` was accepted — it was refused; #1355 said
  "always the next word"). The review caught the second. Read the sentence as a rule an agent will follow literally.
* **`gh pr merge` was often already done** — the founder merges from their side; check `mergedBy` before writing.
* **bsvibe MCP can vanish mid-session** (`CREDENTIAL_STORE_UNAVAILABLE`, keychain); no GitHub writes until it is back.

## §Ⅱ-l — What 11-11/11-13 found

* **#1373 stopped intake for about five hours** (11-13, `github_app_webhook_unbound` on every delivery). Routing began
  to require `installation.id` to equal the account's recorded installation, and the product's own connector was bound
  before installations were recorded — it had none. I had checked the per-account webhook path only. Issues opened in
  that window were dropped and had to be re-submitted. Fixed by hand (#1383). A guard that compares against a recorded
  value must be checked against the production rows that predate the record; after the deploy, read the backend log for
  the guard's rejection event, not just the tests.
* **Issue premises were wrong three more times** (#1369 assumed a client-side plugin path; #1376 and #1382 assumed the
  install claim recorded nothing — `claim_install` has written `external_ref` since 09-10). #1382's run built a change
  for a gap that did not exist. Read the code before writing "X does not …".
* **Editing an already-applied migration does nothing in production** — alembic never re-runs a revision (#1382's
  card). A data fix goes in a new revision. A migration that calls an external API at deploy time is a risk of its own.
* **One run spent 2,001,290 tokens on automatic merge-conflict retries** (`5b8cb753`): the merge watch's counter resets
  on every new conflicting head, so an agent that pushes each retry never reaches it (#1387).
* **A shipped run's pending decisions could not be closed safely** — `discard` cancels the run, `retry` re-drives it
  (#1386). Three sat on `5b8cb753` until #1388's migration closed them.
* **The review keeps leading the denials.** Every denial 11-12/13 was led by a review finding checked against the
  code; one high was overstated (bounded by another cap), none was false.
* **Verify a raw-SQL migration on Postgres with the production driver.** #1388's sweep compares enum columns with
  string binds; it was run once on a throwaway `postgres:16-alpine` over asyncpg (correct rows closed, exclusions
  kept). That container ran on `colima` for under a minute and was removed — §Ⅳ has one bullet against throwaway
  containers on this box and one describing them; 형님 to decide which stands.
* **A Monitor filter without the failure events goes silent** — a 30-minute watch on a run listed only success and
  card events and delivered nothing while the run failed two rounds. Include `verification_failed` / `run_failed`.

## §Ⅲ — Next

1. **Close #1387 when #1389 merges** (pause window). Small follow-up: the merge watch clears the whole conflict budget
   when a PR turns `clean`, including the `exhausted` mark; a conflict that resolves itself and recurs before the
   founder answers would re-open the run.
2. **Not ready for strangers — the 11-12 audit's remaining blockers are not filed yet** (none is security):
   schedules have no count / interval floor / overlap guard and fire after a workspace is deleted · unlimited
   workspaces per user multiply every per-workspace limit · 2 global drive slots and 1 verify slot, FIFO across
   tenants, while plans promise 3 / 10 runs · the GitHub App is private (strangers cannot install it) · sign-up has no
   abuse control and runs on Supabase's default SMTP limit · the alert path is one person (the 5.5-day auth outage) ·
   no off-box copy of the KMS / MCP OAuth signing keys · restore never rehearsed, 24 h recovery point.
3. **11-16 weekly BStockReport**: ships with one Telegram message and no `delivery_failed` (#1340); tick
   `requirements-ledger-checklist.md` D.
4. **#1323 leftovers**: `mcp_work_claim_lost` firing for a turn that survives its job; a run paused on a founder
   decision across a restart. Then the rest of the #929 second-box list (`docs/runbook/vault-to-postgres.md`).
5. **R5 stage 2 decision** — once ≥ 20 reviewed cards, compare deny rates with / without a medium-or-high finding.
6. Once `leftover_dirs` has a sample, decide **#1267 layer 2**.
7. Low priority: `git grep -w` counts common names as called; `--cov ./tests/x` is read as `--cov`'s value; a vague
   denial reason re-judges every ledger item; the sandbox image has no `BSVIBE_PARSER_OFFLINE`; #1380's review lows
   (the stop-reason test checks only the no-stop case; every `OSError` is counted as a symlink skip).
8. ⏳ 형님's batch: codex live run (#1183) · #1046 live group-chat tap · #937 outside the repo · the infra items in
   the private audit report · confirm the key escrow and the alert path above.
9. E2E checklists still open: `requirements-ledger-checklist.md` (A1, B, C2, D1/D2/D3, E), `ci-parity-checklist.md`
   (C, E1/E3, F), `counterfactual-guard-checklist.md`, `intake-trust-by-repo-permission-checklist.md` (non-writer
   comment), `worker-restart-releases-claims-checklist.md` (claim-lost, paused run).

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
* **📏 `_drive_loop.py` is at 598 / 600** (`test_h2a_decomposition`). New loop logic goes to its own module (`run_inbox.py` is the shape).
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
* **📺⭐ A Monitor pipeline must not end in `cut`.** `grep --line-buffered … | cut -c1-300` buffers until exit — a
  30-minute watch delivered nothing while the run started and finished (#1290). Use `grep -o` or no trailing filter.
* **🧾⭐ `… | tail -1` hides pytest's exit status.** A gate "passed" with `1 failed` in its last line and a commit
  followed. Capture `pytest`'s own `$?`, then print the tail.
* **🐚 Never name a shell function after a command.** A `cut()` helper shadowed `/usr/bin/cut` later in the same
  shell and wrote a stray copy of a source file.
* **🧷⭐ A test that passes before the fix is measuring something else.** #1298's first test stayed green without the
  fix — another `continue` path (the no-work nudge) caught the edit. Make the test hold production's exact shape.
