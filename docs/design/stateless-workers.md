# Stateless workers — a run can move between machines (#929, option B)

Status: **proposal**, 2026-10-20. Founder chose B ("state lives in R2, any machine can take over") over A ("pin a run to the machine that started it").

## 1. What ties a run to one machine today

Measured in code, 2026-10-20.

| State | Where it lives | Portable? |
|---|---|---|
| Run row, messages, decisions, work-tool state | Postgres | ✅ — resume rebuilds the conversation from the DB (`_loop_context.py`) |
| Product repo (`var/products/<pid>`) | local disk, published to R2 as a git bundle **at ship** | ✅ since #1188 (a present repo follows the bundle) |
| **Run worktree** (`var/runs/<rid>`, branch `bsvibe/run/<rid>`) | **local disk only** | ❌ |
| Run artifacts (`var/artifacts`), knowledge vault, skills | local disk only | ❌ (stages 2–3) |
| Sandbox container | the machine's DinD, keyed by run | per machine by design |

The run worktree is the blocker:

- **Uncommitted until verify.** Every edit the agent makes before the first verify, and between failed verify rounds, is uncommitted. The first commit is verify's `commit_worktree` (`verification_service.py:616`).
- **Run-branch commits are never in R2 mid-run.** They live only in this machine's `var/products/<pid>/.git`, because the bundle is published at ship.
- **A second machine silently loses the work.** `add_run_worktree` runs `worktree add -b bsvibe/run/<rid> main` whenever the path is missing (`product_workspace.py:565`). A second machine that claims the run starts a fresh branch from `main`: the work is lost without an error.
- **Claims carry no machine.** `claimed_by` is a random uuid per process (`agent_worker.py:260`), so nothing says which machine holds a run's tree.

### The second problem: tool calls do not run where the worker runs

An executor agent acts through the MCP work tools. Those execute in the **API process** that received the MCP request (`mcp/tools/work_registry.py:_sandbox_for`). That process reaches the worktree through the same disk and the same DinD the worker uses.

With several backend replicas, the replica answering a tool call may not hold that run's tree. A turn makes dozens of tool calls a minute, so moving the tree through R2 per call is not an option.

## 2. Design

### 2.1 A run has a home while it runs

- `execution_runs.home_machine` is set on claim from `BSVIBE_MACHINE_ID` (the same id #1189 introduced for verify slots). It is cleared on release.
- **Tool calls go home.** An MCP work-tool call for a run whose home is another machine is forwarded there.
- **The forwarding mechanism already exists.** A `client_attach` run's commands are dispatched to the founder's machine over Redis streams and awaited (`ClientWorkerSandboxManager` → `executors.dispatch`). The same substrate addresses a backend machine: a per-machine stream (`bsvibe:machine:<id>`) consumed by a small handler in each backend process that runs the work tool locally and replies.
- When the home is this machine, or unset on a single-box deployment, the call runs in place, exactly as today.

### 2.2 Checkpoint when a run leaves a machine

`checkpoint_run_worktree(run)` does two things:

1. It commits a WIP snapshot on the run branch, through `commit_worktree`. That function already excludes `.venv`, caches and the scaffold.
2. It uploads `git bundle create <file> main..bsvibe/run/<rid>` to the store under a run key, `runs/<rid>.bundle` (a second key namespace in the bundle store). The resulting sha is recorded on the run (`payload.checkpoint_sha`).

It is called at every point where a run is released by the machine holding it:

- a pause (question, token cap, round cap);
- a capacity yield;
- a drive failure released back to OPEN;
- the stale-claim reaper, run on the **owner** before the claim is cleared where possible. A crash cannot checkpoint, which is what 2.4 covers.

A per-turn checkpoint is optional. It bounds crash loss at the cost of one upload per turn, and is measured before it is switched on.

### 2.3 Restore when a run arrives on a machine

In `_product_workspace_provisioner`, before `add_run_worktree`, if the worktree is missing:

- **A checkpoint exists:** fetch it into `refs/heads/bsvibe/run/<rid>`, then run `worktree add <path> bsvibe/run/<rid>` with no `-b`.
- **The run recorded a `checkpoint_sha` but nothing can be restored:** **refuse** with a Decision. Never branch from `main` for a run that already did work.
- **A genuinely new run:** `-b` from `main`, as today.

GitHub-delivered runs do the same with a pushed run branch (`bsvibe/run-<short>`), which the merge watch already re-clones (`merge_watch_server_freshen.py:62`).

### 2.4 Crashes

A machine that dies mid-turn cannot checkpoint. The run is reaped back to OPEN as today, and its last checkpoint is the restore point. Work since the last checkpoint is lost and is **reported, not hidden**: the resumed run is told what its tree was restored to.

This is the trade the per-turn checkpoint buys down.

### 2.5 Cleanup

`remove_run_worktree` deletes `runs/<rid>.bundle` on the machine that holds the tree. A run that finished with no tree anywhere (its machine died after a checkpoint, then the run was cancelled) is reached by the worker's periodic sweep, `reap_terminal_run_checkpoints`. The store cannot list, so the DB drives it: a terminal run that still records a checkpoint has the bundle deleted, then the record dropped. A failed delete keeps the record, so the next sweep retries.

## 3. Out of scope here

- **Run artifacts, the knowledge vault, skills** (stage 2–3). The vault holds Markdown as the source of truth, and needs its own design.
- **`client_attach` runs.** Their tree is on the founder's machine and their tool calls already go there.
- **Global sandbox concurrency** (stage 4). Each machine bounds its own DinD.

## 4. Order of PRs

1. `home_machine` on claim and release.
2. Checkpoint at release, plus the run key namespace in the bundle store.
3. Restore on provision, with refuse-instead-of-fresh-branch.
4. Tool-call forwarding to the home machine.
5. Cleanup and reaper.

PRs 1–3 make a pause/resume across machines safe. PR 4 makes concurrent replicas safe while a turn runs.

**A second machine must not be added before 1–4 are live.**
