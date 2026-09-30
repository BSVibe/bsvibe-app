# BSVibe — System Overview

A map of how BSVibe works end to end: what runs where, how one unit of work
travels from a trigger to a merged PR, and how the product drives coding agents.
Read this before changing anything that crosses two stages.

> **How to read this document**
> * §1–§9 describe **structure**. They cite files and function names, not line
>   numbers, so they survive edits. If a citation stops resolving, the section is
>   stale — fix it, do not work around it.
> * §10 is a **dated snapshot** (prod numbers + the open-issue map). It rots by
>   design; refresh it in place and bump its date.
> * Current state and priorities live in [STATUS.md](./STATUS.md) and
>   [HANDOFF.md](./HANDOFF.md). Open work lives in GitHub issues, not here.
> * Where this document and the code disagree, the code wins — then fix this file.

---

## 0. One paragraph

BSVibe is an **AI agent OS for one founder running many products at once**. Work
enters through several doors (Direct, GitHub, schedules, chat connectors) and
converges on a single pipeline: **trigger → request → frame → agent loop →
BSVibe-run verification → Safe Mode → delivery → knowledge**. The coding itself is
done by a CLI agent (Claude Code in practice) on the founder's machine, but
**BSVibe owns the tools, the prompt, the loop and the verdict** — the agent is a
component, not the orchestrator.

## 1. Product philosophy (from the architecture docs)

Sources: [architecture/strategy-synthesis.md](./architecture/strategy-synthesis.md),
[architecture/ux-design.md](./architecture/ux-design.md).

* **Glass box (Bet B).** Trust comes from transparency and control, not from the
  agent being perfect. The founder supervises imperfect agents through a surface
  that shows what they did and why.
* **Trust ratchet — the spine.** Two one-way loops: the work loop (plan → act →
  verify; a failure re-plans, never undoes) and the knowledge base (only grows).
  A mistake is supervised once, captured, and retrieved by later runs, so the
  supervision burden decays.
* **The founder never writes knowledge by hand.** It is extracted from what they
  direct, decide, correct and review, and from existing repos.
* **Six moments:** Direct · Passive trigger · Glance · Decide · Review (proof) ·
  Inside (knowledge).
* **North star:** founder touch time. Never an admin console — plain-language
  status, no metric cards.

## 2. Physical topology

```
Founder ── PWA (app.bsvibe.dev, Vercel) · MCP clients · Telegram · GitHub
                │
        api.bsvibe.dev (Cloudflare tunnel → Mac mini)
┌─ Mac mini: docker compose (deploy/compose.yaml) ───────────────────────┐
│ backend      API + MCP server (/mcp); runs `alembic upgrade head` on    │
│              every boot (entrypoint)                                    │
│ worker       `python -m backend.workers` — the background loops (§3.8)  │
│ postgres     pgvector pg16; RLS fail-closed (backend/data/rls.py)       │
│ redis        stream notifications only — the DB row is the truth        │
│ sandbox-dind isolated Docker daemon where agent file/shell ops run      │
└─────────────────────────────────────────────────────────────────────────┘
┌─ Mac mini host: launchd ───────────────────────────────────────────────┐
│ executor workers (backend/executors/worker/main.py)                     │
│   register → heartbeat → poll → claim → run `claude --print …` → result │
│   ⚠ NOT autodeployed: after a merge, pull + `launchctl kickstart -k`    │
└─────────────────────────────────────────────────────────────────────────┘
```

* **Two different things are called "worker".** The compose `worker` service runs
  BSVibe's own background loops. The *executor worker* is a host daemon that
  launches coding CLIs. They never share a process.
* **Deploys differ by surface.** backend + worker: autodeploy poller rebuilds on
  `main`. PWA: Vercel on merge. Host executor workers: manual restart.

## 3. Lifecycle of one unit of work

```
 PWA/MCP Direct ─┐  GitHub webhook ─┐  Schedule tick ─┐  chat connector ─┐
 (cap + budget)  │  (no cap)        │  (no cap)       │  (no cap)        │
                 ▼                  ▼                 ▼                  ▼
        trigger_events   (idempotent on workspace, source, idempotency_key)
                 │  IntakeWorker: receive() → binding lookup + trigger.filters
                 ▼
        requests (open)  + founder's original text recorded to the vault
                 │  AgentWorker.claim_once → AgentRunner.open_run
                 ▼
        execution_runs (open)
                 │  AgentWorker.drive_once:
                 │    ① frame  (one LLM call; may split into a step chain)
                 │    ② RunOrchestrator loop (rounds; §4)
     ┌───────────┼─────────────────────┬──────────────────────┐
     ▼ verified                        ▼ needs_decision       ▼ system_error
 review_ready + Deliverable        running + pending         failed
 + delivery_events + settle        Decision (checkpoint)
 + next step run (if chained)
     │
     ▼  ③ DeliveryWorker: Safe Mode? → queue → founder approves → dispatch
     │     (GitHub PR / Telegram / Notion / …) → delivery success → shipped
     ▼  ④ MergeWatchWorker: PR checks green → squash merge
     ▼  ⑤ SettleWorker: settle activity → garden note → canon promotion
```

### 3.1 Entry points
* **Direct** — PWA `POST /messages` (`backend/api/v1/messages.py`) and MCP
  `bsvibe_direct` (`backend/mcp/tools/direct_tools.py`). Both enforce the
  concurrent-run cap and the monthly token budget, then write a trigger via
  `DirectTrigger`.
* **PWA `/ask`** answers inline through `DirectAnswerService`
  (`backend/workflow/application/direct_answer.py`) — no trigger, no run — unless
  the model decides it is work.
* **Webhooks** — `backend/api/webhooks.py`: the per-connector route
  `/api/webhooks/{connector}/{token}` and the GitHub App route
  `/api/webhooks/github` (App webhook secret; target resolved from the repo's
  **binding**, never guessed from `repo_url`). Both land through `_land_trigger`.
  Self-authored PRs (`bsvibe/run-…`, `bsvibe/run/…` branches) are skipped in
  `plugin/github/webhook.py`.
* **Schedules** — `backend/schedule/`: `instruction` (text task) and
  `product_tick` (look at goal/knowledge/history, do the single most valuable
  next action). Both write a trigger.
* **Not gated:** webhooks, schedules and chained next steps bypass the run cap and
  token budget **on purpose** — see the module docstring of
  `backend/workflow/application/run_caps.py` for the argument and its stated
  limit.

### 3.2 Intake
`IntakeWorker` (`backend/workflow/infrastructure/workers/intake_worker.py`) claims
undrained triggers (cross-tenant read), runs `receive()`
(`backend/workflow/application/stages/intake.py`): a webhook looks up its
`resource_bindings` row and applies `trigger.filters` (key-equality AND on the
payload). A filtered trigger is stamped `_received_filtered` and produces no
request. A passing one copies `product_id`, `binding_id`, `selection` onto a new
`requests` row.

### 3.3 Request → run
`AgentWorker` (`backend/workflow/infrastructure/workers/agent_worker.py`) ticks:
apply queued answers → claim requests (`AgentRunner.open_run` in
`backend/workflow/application/agent_runner.py`) → drive runs. Only two places
create an `ExecutionRun`: `open_run` and the next-step spawner in the same file.

### 3.4 Framing and step chains
One LLM call (`backend/workflow/application/stages/frame.py`) classifies the
request and may split it into `frame.steps` — **only** when the founder has stage
routing rules. Step 0 runs in the current run with `payload.step_intent`; on
`review_ready` the next step is spawned as a **new run** that is not re-framed.
The agent's prompt carries the founder's full text plus
*"This run is ONE step of that request. Your part of it: …"*
(`_intent_directive` in `backend/workflow/application/_loop_context.py`).

### 3.5 Driving a run
There is **one orchestrator**, `RunOrchestrator`
(`backend/workflow/application/agent_loop.py`, loop body in `_drive_loop.py`).
The model account is resolved per caller (`backend/dispatch/resolver.py`:
routing rules by priority → workspace default → a `no_model_account` Decision).
A **round** is one `llm.complete()` call; what that call *is* depends on the
account (§4). Bounds: `execution_work_round_budget` (48, narrowable by the
agent's declared budget — `round_budget.py`) and `agent_max_run_tokens`
(2,000,000 — `token_budget.py`).

### 3.6 Verification — BSVibe runs it, not the agent
`VerificationService.verify` (`backend/workflow/application/verification_service.py`):
1. commit + merge `main` (conflict → fail);
2. **repo gate** — an LLM derives the repo's own check commands from its
   manifests/CI, BSVibe **executes them in the sandbox**, exit codes decide;
   underivable → fail closed;
3. outcome-demonstration probes;
4. LLM judge — grades **only the agent's own declared criteria**;
5. `verified` only if gate, judge and demonstration all pass.

No declaration: nothing changed → pass; otherwise the agent is nudged
(`undeclared_verification.py`). client_attach runs the gate on the founder's
machine (`inplace_gate.py`).

### 3.7 After verified
* `write_verified_deliverable` (`backend/workflow/domain/verified_deliverable.py`)
  writes the Deliverable, a `delivery_events` row and a settle activity.
* `DeliveryWorker` (`backend/workflow/infrastructure/workers/delivery_worker.py`)
  queues for Safe Mode if the workspace flag is on, the run is `product_tick`, or
  the binding's `output_mode` is `safe`; otherwise dispatches.
* Approval (`safe_mode_approval.py`) → connector dispatch (GitHub PR on a
  `bsvibe/run-<8hex>` branch — `delivery/connector_dispatch/_github.py`) →
  `auto_resolve_run_on_delivery` (`run_delivery_resolution.py`) → `shipped`.
* Deny has two kinds: `rejected_approach` (reason teaches the next run, reopens
  this one) and `queue_cleanup` (recorded, teaches nothing) —
  `safe_mode_queue.py`.
* Merge-watch (`backend/workflow/application/runtime/merge_watch_runtime.py`):
  CI green → squash merge; conflict → re-dispatch; stall → Decision.

### 3.8 Background loops (compose `worker`)
Registered in `backend/workflow/application/runtime/worker_runtime.py`:

| Loop | Cadence | Does |
|---|---|---|
| IntakeWorker | 5 s | triggers → requests |
| AgentWorker | 5 s | answers → claim → frame → drive; reaps stale claims and run dirs |
| DeliveryWorker | 5 s | delivery_events → Safe Mode queue or dispatch |
| SettleWorker | 5 s | settle activity → vault notes → canon promotion |
| NotifyWorker | 5 s | notification outbox → push channels (quiet hours) |
| RelayWorker | 5 s | audit_outbox → relay |
| ScheduleWorker | 10 s | cron schedules → triggers |
| MergeWatchWorker | 30 s | PR checks → merge (flag-gated) |
| retraction sweep | 60 s | retraction tombstones |
| AuthDependencyWorker | 300 s | JWKS probe |
| DailyBriefWorker | 600 s | daily digest |
| Safe Mode expiry | 3600 s | expire queue items |
| audit retention sweep | 86400 s | delete old audit rows |

## 4. How BSVibe drives Claude Code

This is the section most people get wrong. **Claude Code inside BSVibe is not the
Claude Code a developer uses in a terminal.**

| | Claude Code in a terminal | Claude Code inside BSVibe |
|---|---|---|
| Tools | built-ins (Read, Edit, Bash, Grep, …) | **all built-ins disallowed**; only BSVibe's 9 MCP work tools (`bsvibe_work_file_read/write/edit/list`, `shell_exec`, `knowledge_search`, `declare_verification`, `ask_user_question`, `emit_deliverable`) |
| File access | local disk | every op goes MCP (HTTP) → backend → DinD sandbox (server_sandbox) or back to the worker as an exec task (client_attach) |
| Session | continuous conversation | **a new `claude` process per BSVibe round**; the whole transcript is re-rendered as text (no `--resume`) |
| Stops when | a human decides | no `--max-turns`; only timeouts (line 3600 s, total 7200 s) and a token ceiling checked **after** the session ends |
| Config | CLAUDE.md, skills, memory | disabled (`--setting-sources ""`, auto-memory off) |

Command shape (`_build_cmd` in `backend/executors/worker/claude_code.py`):
`claude --print --output-format stream-json --verbose --strict-mcp-config
--mcp-config <0600 file> --allowedTools <bsvibe_work_*> --disallowedTools
<every built-in> --setting-sources "" --append-system-prompt <system> --model …`.
The worker aborts if the tool set the CLI reports at init is not exactly the
allowlist.

**Two loops, nested.** One BSVibe round = one executor task = one whole
autonomous Claude Code session, which may itself take many internal turns. The
session returns text with no tool calls, so the BSVibe loop always treats it as
"done" and verifies; on failure it appends "Verification FAILED" and starts a
**fresh** session. The same `round_budget` therefore means "48 model calls" on a
LiteLLM account and "48 whole sessions" on an executor account.

**Where it runs** (`execution_target` on the product):
* **server_sandbox** — product repo restored from its R2 git bundle, worktree
  `var/runs/<run_id>` on branch `bsvibe/run/<uuid>`
  (`backend/storage/product_workspace.py`), mounted into a DinD container
  (`backend/workflow/infrastructure/sandbox/docker_manager.py`).
* **client_attach** — the founder's real repo, worktree `<repo>/wt/<short>` on
  branch `run/<short>`; every file/shell op becomes an exec task back to the
  worker. The server never stores the source.
* **GitHub-bound delivery** — clone, `bsvibe/run-<8hex>` branch, PR.

**Other executors.** `opencode` works through `opencode serve` with a per-run MCP
registration. `codex` cannot be restricted to BSVibe's tools, so
`backend/dispatch/adapter.py` refuses agentic work routed to it.

**Token metering.** `_claude_extract_usage` sums `input + cache_creation +
cache_read` into one prompt number, read from the session's final `result`
event. Cache reads are counted at full weight against the per-run ceiling.

## 5. Decisions (checkpoints)

A run that needs the founder pauses as `running` with a pending Decision;
resolving it (`backend/workflow/application/checkpoint_resolution.py`) reopens
the run with the answer folded in.

| Kind | Actions |
|---|---|
| `ask_user_question` | free-form options |
| `verification_failed` | ship / retry / discard |
| `run_drive_failed` | retry / discard |
| `merge_conflict_review` | retry / discard |
| `merge_watch_stalled` | acknowledge |
| `run_token_cap_reached` | **none** |
| `no_model_account`, `ambiguous_model_account` | **none** |

Kinds with no actions can only be escaped by discarding the run.

## 6. Knowledge

* **Vault** — markdown notes per workspace are the source of truth; notes grow
  seedling → budding → evergreen; recurring patterns promote to **canon**
  (`backend/knowledge/`).
* **Into runs** — relevant canon is seeded at loop start; the agent can call
  `knowledge_search` mid-run (≤5 results).
* **Out of runs** — `SettleWorker`
  (`backend/knowledge/infrastructure/workers/settle_worker.py`) writes a note only
  for decisions, reasoned rejections, or knowledge the agent explicitly declared
  in `declare_verification`. Routine work writes nothing.
* **Bootstrap** — adding a product walks its repo into the knowledge base.
* **Inside** — the PWA Knowledge page renders the concept graph. Trust metrics
  exist in the API (`/api/v1/inside/trust/fleet`, `/api/v1/inside/trust/{product_id}`)
  but no PWA screen reads them.

## 7. Surfaces

* **PWA** (`apps/pwa`) — primary nav is Brief · Knowledge · Skills. Brief holds
  Working now / Needs you (Safe Mode items, checkpoints, questions, proposals) /
  Shipped. Settings: general (incl. Safe Mode), models, connectors, schedules,
  notifications.
* **MCP** (`backend/mcp`) — ~80 founder tools (`bsvibe_*`). A run-scoped token
  sees only the 9 work tools and is refused everything else.
* **Connectors** (`plugin/*`) — github, telegram, slack, discord, notion, email,
  sentry, obsidian, claude, gpt (linear, trello hidden). A **binding**
  (`resource_bindings`) ties one product to one connector resource with
  `selection`, `trigger.filters` and `output_mode`.
* **Notifications** — outbox rows written in the same transaction as the event;
  delivered in-app + to bound push channels, respecting quiet hours.

## 8. Tenancy and limits

* **Isolation, three layers** — request context variable, ORM auto-filter, and
  Postgres RLS on `app.current_workspace_id` (`backend/data/rls.py`). RLS is
  **fail-closed** since migration `rls_fail_closed`: an empty setting sees 0 rows.
  Background paths that must cross tenants use `cross_tenant_read`.
* **Concurrent runs** — `workspaces.max_concurrent_runs` (default 3, NULL =
  uncapped) counts every non-terminal run **including `review_ready`**.
* **Monthly token budget** — `workspaces.monthly_token_budget`, checked at the two
  Direct entry points only.
* **Per-run token ceiling** — `agent_max_run_tokens`, checked after each round.

## 9. Glossary

| Term | Meaning |
|---|---|
| trigger | an inbound event row (`trigger_events`), idempotent |
| request | the founder's ask after intake (`requests`) |
| run | one attempt to do the work (`execution_runs`) |
| round | one `llm.complete()` in the BSVibe loop — a model call or a whole CLI session (§4) |
| executor task | one CLI job handed to a host worker (`executor_tasks`) |
| frame | the one-call classification of a request; may yield `steps` |
| step chain | a request split into sequential runs |
| binding | product × connector resource, with filters / selection / output mode |
| Safe Mode | hold every deliverable for founder approval |
| settle | turning a run's decisions and lessons into knowledge |
| canon | promoted, retrievable knowledge |
| proof_state | `untested` / `proved` on a work step |

---

## 10. Snapshot — 2026-09-30

*Refresh in place; change the date. Numbers are prod, read-only.*

### 10.1 Prod numbers

| | |
|---|---|
| Runs, all time | 309 — shipped 27 (8.7%) · cancelled 226 · failed 56 |
| Top cancel reasons | "abandoned review backlog cleared before the free-plan run cap" 73 · orphan cleanup (deleted product) 22 |
| Top failure reason | "frame could not classify the request" 20 (mostly July, before the bounded retry #897) |
| Triggers, last 30 days | direct 52 · github 7 · schedule 4 · telegram 2 |
| Executor tasks, last 30 days | claude_code 683 · opencode 7 · codex 0 |
| Metered agentic claude_code tasks | 36 — prompt tokens p50 33k · p90 427k · max 8.9M |
| Products / workspaces | 4 / 4 (effectively one founder) |

### 10.2 What a real engineering task looked like (issue #1102)

The GitHub App opened a run for #1102 (a concurrency bug). The framer split it
into two chained runs: *write a failing test* / *fix*. The first Claude Code
session did its step correctly (red test reproducing the race) in 7 minutes and
**3,055,575 prompt tokens**, then the per-run ceiling (2,000,000) stopped the
chain before the fix could start. The agent was capable; the harness around it
was the limit. Cancelling another run did not stop its live session, which ran
11 more minutes and 6.4M tokens.

### 10.3 Open-issue map

| Area | Issues |
|---|---|
| **How BSVibe wraps Claude Code** (the central design decision) | #1103 step split · #1104 ceiling unit · #1114 no in-session bound |
| Cancellation and the run state machine | #1102 cancel overwritten · #1106 cancel does not stop the session · #1109 `shipped` reopens · #1110 six writers bypass `transition()` |
| Isolation | #1107 sandbox container shared per product |
| Delivery gates | #1108 next step loses Safe Mode gates · #1111 "shipped" notification fires early · #1112 checkpoint `ship` opens no PR |
| Checkpoints | #1105 dead end (same family as #1074) |
| Operations | #1113 every issue becomes a run, outside caps · #1115 `review_ready` occupies the cap · #1116 codex leftovers, three branch schemes |

**The decision most of these hang on (§4):** keep driving Claude Code as a
tool-restricted, one-shot session per round, or lean on Claude Code's own loop
(built-in tools, session continuity). It trades isolation and the client_attach
privacy contract against cost and speed; #1103, #1104, #1106 and #1114 resolve
differently depending on the answer.
