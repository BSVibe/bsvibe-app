# GitHub CI-green auto-merge + concurrent-work conflict handling

BSVibe can develop a github-bound product (e.g. bsvibe-app itself) with several
work items running against **one repo at the same time**. This document
describes how a PR opened by github delivery is automatically merged once its CI
is green, and how concurrent PRs that would conflict are handled — mechanical
conflicts auto-resolve, ambiguous ones escalate to the founder.

Gated by `github_auto_merge_enabled` (default **off** → the pre-existing "open
the PR, a human merges it" behavior is byte-identical).

## Why it exists

A github product's run clones the repo, works on a `bsvibe/run-<id>` branch,
then delivery `commit → push → open_pr`. With multiple concurrent runs that
yields multiple PRs; the first to merge advances `main`, and the rest are now
behind — and, if they touched the same code, conflict. Without auto-merge the
founder merges every PR by hand; without conflict handling the later PRs stall
unmergeable. This feature closes both gaps while keeping the founder in the loop
for genuine judgment calls only.

## The pieces

| Piece | Where | Role |
| --- | --- | --- |
| `merge_pr` / `get_check_runs` | `plugin/github/client.py` | PR merge + CI-status API surface |
| `github_repo_lock(session, repo)` | `backend/storage/github_repo_lock.py` | per-repo advisory lock — serializes merges on one repo |
| `github_merge_watch` table + repo | `backend/workflow/infrastructure/github/` | durable one-row-per-watched-PR poll queue |
| `MergeWatchWorker` | `backend/workflow/infrastructure/workers/merge_watch_worker.py` | polls the queue, runs the state machine |
| `GitOps.fetch(+unshallow)` / `merge_ref` | `backend/workflow/infrastructure/delivery/git_ops.py` | freshness merge of `origin/<base>` into the run branch |
| `merge_watch_runtime` | `backend/workflow/application/runtime/merge_watch_runtime.py` | wires the worker (gated) + injects the client resolver + the run re-dispatch callback (keeps the infra worker off the `plugin.github` / `AgentRunner` import surface) |
| `merge_conflict_review` Decision | `backend/workflow/application/_checkpoint_shared.py` | the ambiguous-conflict escalation to the founder |

## Lifecycle of one PR

1. **Enqueue.** After `deliver_github` opens the PR (only when the safe-mode gate
   said *deliver* — see below), a `github_merge_watch` row is inserted
   `pending_ci`, `next_poll_at=now`, `deadline_at=now+github_auto_merge_ci_deadline_s`.
2. **Watch.** `MergeWatchWorker` claims due rows (`FOR UPDATE SKIP LOCKED`,
   at-least-once) and reads `GET /pulls/{n}` → `mergeable_state`:
   - `clean` → **merge step**: under `github_repo_lock`, re-confirm `clean`, then
     `PUT /pulls/{n}/merge` (squash). Done → `merged`.
   - `blocked` / `unstable` / `unknown` → CI still running → back to `pending_ci`
     with exponential backoff. Past `deadline_at` → `failed` (CI never went green).
   - `behind` / `dirty` → **freshness merge** (below). GitHub's label is not
     trusted; the merge is done locally to decide clean-vs-conflict authoritatively.
3. **Freshness merge** (per-repo lock held, so PR #2 always freshens against PR
   #1's just-merged `main`): in the run's clone, `fetch origin/<base> --unshallow`
   then `merge <origin/base>` into the run branch.
   - **clean** → `push` the freshened branch → `pending_ci` (CI re-runs on the new
     head; a later clean poll merges).
   - **conflict** → hand off to the agent (below).
4. **Restart-safe / idempotent.** All state is on the row (`attempts`,
   `next_poll_at`, `deadline_at`, `conflict_dispatched`). A crash mid-merge is
   safe — the next claim re-reads the PR; an already-merged PR maps to `merged`.

## Conflict handling — clear auto-resolves, ambiguous asks the founder

On a freshness-merge **conflict** the worker marks the row `needs_resolution`,
records the conflict head, writes `run.payload["merge_conflict"] =
{conflict_paths, base_branch, pr_number}`, and re-dispatches the run
`RUNNING → OPEN` (once per conflict head — `conflict_dispatched` guards against
infinite re-dispatch). The AgentWorker re-drives the run; the drive loop surfaces
the conflict to the agent, which then decides:

- **Clear / mechanical** (imports, adjacent non-overlapping edits, formatting):
  the agent resolves it in the clone, commits, and the run re-delivers — the
  push updates the existing PR's head. The `needs_resolution` row re-freshens on
  the next poll, is now clean, and merges. **No founder involvement.** "Clear" is
  *demonstrated by a clean re-merge*, never asserted.
- **Ambiguous** (two changes touched the same logic; the correct merge needs a
  human judgment call): the agent does **not** guess — it raises a
  `merge_conflict_review` Decision, which pauses the run and reaches the founder
  on telegram via the existing checkpoint-notification path. The founder's answer
  (`retry` with guidance, or `discard` to close the PR) resumes the run through
  the standard resolve-checkpoint path; the agent applies the guidance, re-pushes,
  and the merge-watch loop completes. A dirty PR can never be force-merged, so
  the resolution always flows back through the agent — there is no new
  merge-completion code path.

## Interaction with Safe Mode

Auto-merge is strictly **downstream of the safe-mode approval gate**. The
`github_merge_watch` row is only created after `deliver_github` opens the PR, and
the PR is only opened when `resolve_output_mode_gate` says *deliver* (not queue).
So when Safe Mode / an autonomous-origin run requires approval, the PR — and
therefore the auto-merge — waits behind the founder's approval. Auto-merge never
bypasses the gate; it only removes the manual PR-merge click after approval.

Note: BSVibe's own `auto_ship_merge_to_main` (the *local* product-mirror
fast-forward) and this github PR merge are mutually exclusive by the
`.git`-file-vs-directory discriminator (`_is_linked_worktree`): a linked
worktree ships to the local mirror, a github clone (a standalone `.git`
directory) delivers via push+PR and is merged by this worker. They never both run.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `github_auto_merge_enabled` | `false` | Master switch. Off → open PR, human merges (unchanged). |
| `github_auto_merge_ci_deadline_s` | `3600` | Wall-clock before a stuck-CI PR is marked `failed`. |
| `github_auto_merge_poll_interval_s` | `30` | Base poll cadence (exponential backoff, capped). |

Merge method is **squash**; the merged branch's commits collapse and the PR's
`Closes #N` cross-link auto-closes the source issue. "Green" is
`mergeable_state == "clean"`, which honors the repo's own required checks /
branch protection.

## Phase 3 alternative (not implemented) — GitHub-native auto-merge

Instead of the poll-based `MergeWatchWorker`, GitHub's native auto-merge
(`enable_auto_merge` on the PR + a branch-protection rule requiring the checks)
would let GitHub merge the PR itself when CI passes — less code, no polling.

Trade-offs / why the poller was chosen:
- **No conflict recovery hook.** GitHub-native auto-merge just *fails* a
  conflicted/behind merge; there is nowhere to run the freshness merge or
  re-dispatch the run to the agent — the core of the concurrent-work requirement.
  The poller owns that flow.
- **No BSVibe-side visibility / serialization.** The durable row + per-repo lock
  give an explicit, restart-safe state machine and cross-PR serialization that a
  GitHub-side toggle does not.

The native path could still be layered on later as a *fast-path for the trivial
clean case* while keeping the poller for freshness/conflict recovery — but the
conflict recovery (Phase 2) would remain worker-owned either way.
