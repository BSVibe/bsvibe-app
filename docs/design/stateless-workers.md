# Stateless workers — a run can move between machines (#929, option B)

Status: **proposal**, 2026-10-20. Founder chose B ("state lives in R2, any machine can take over") over A ("pin a run to the machine that started it").

## 1. What ties a run to one machine today

Measured in code, 2026-10-20.

| State | Where it lives | Portable? |
|---|---|---|
| Run row, messages, decisions, work-tool state | Postgres | ✅ — resume rebuilds the conversation from the DB (`_loop_context.py`) |
| Product repo (`var/products/<pid>`) | local disk, published to R2 as a git bundle **at ship** | ✅ since #1188 (a present repo follows the bundle) |
| **Run worktree** (`var/runs/<rid>`, branch `bsvibe/run/<rid>`) | **local disk only** | ❌ |
| Run artifacts | the run worktree (`var/runs/<rid>`) — there is no separate artifact store | ✅ with stage 1, except reads from another machine (§5.4) |
| Knowledge vault (`var/vault`), skills (`var/skills`) | local disk only | ❌ (stage 2, §5) |
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
- **How a call goes home** (`backend/mcp/home_forwarding.py`). The `client_attach` substrate forwards shell commands to a worker that polls over HTTP. A backend machine can do better: it forwards the whole tool call, which runs through the same executor as a local one.
  - The caller `XADD`s the call (tool, arguments, the principal it was authorised as, a deadline) to `bsvibe:machine:<home>` and `BLPOP`s `bsvibe:machine:reply:<request>`.
  - Each API process serves its own machine's stream through one consumer group, so any replica on that machine can answer; they share its disk.
  - A refusal at home comes back as the same refusal. A crash comes back as a generic failure.
  - **At most once.** A request is acknowledged as it is read and never redelivered, because a write or a shell command is not idempotent. A request whose caller already gave up is dropped, not run late.
  - A forwarded call runs at home with `forwarded=True` and never bounces on. The run-scope refusal is checked again there.
  - A home that does not answer within the longest allowed command plus slack fails the call, naming the machine. The call never silently runs on a machine without the tree.
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

- **The knowledge vault and skills** — stage 2, §5.
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

## 5. Stage 2 — the vault and skills live in Postgres

Decided 2026-10-21 (founder): **Postgres rows, Markdown unchanged.** The alternatives were R2 objects (no LIST in our client, a full LIST + GET on every index rebuild, concurrency by conditional PUT) and a shared filesystem mount over R2 (one more component to run, file locking unlike a local disk).

### 5.1 What is there today

Measured 2026-10-21.

- **The vault is files, and the files are the source of truth.**
  - Layout: `var/vault/<region>/<workspace_id>/…` holds Markdown with YAML frontmatter, plus `code_graph/*.json` and `.bsage/ontology.yaml`.
  - Postgres holds only derived or queue state: `note_embeddings` (vectors keyed by vault path), `ontology_corrections` (retractions) and `settle_drains`. Prod has about 1,400 notes, a few MB per workspace.
- **Two processes write it concurrently.** The API process writes product bootstrap, MCP note creation, decision resolution and connector imports. The worker process writes settle, intake originals and the retraction sweep. The only locks are `asyncio.Lock`s inside one process (`writer_core/_io.py` `_garden_lock`, `canonicalization/lock.py`). That this has not bitten yet is luck, not design.
- **A seam exists but is bypassed.**
  - `knowledge/graph/storage.py` defines `StorageBackend` (`read`, `write`, `delete`, `exists`, `list_files`, `content_hash`). Its only implementation is `FileSystemStorage`.
  - Measured again while moving them: about 95 file operations in 27 knowledge files, plus the MCP, `/inside` and runtime callers, go around it with `Path` I/O. Examples: `writer_core/_io.py`, `_mutation.py`, `originals.py`, `ingest/file_index_reader.py`, `code_graph/graph.py`, `retrieval/ontology.py`, the MCP knowledge, skills and graph tools, and `/api/v1/inside`.
- **Search rebuilds from the files.** `CanonicalizationIndex.initialize` scans every file on each construction. Semantic search returns vault paths from pgvector, then reads the files.
- **Skills are files too.**
  - Layout: `var/skills/<workspace_id>/<slug>.md`.
  - The API process writes them (REST and MCP). The worker reads them for framing and `invoke_skill`, and they are never materialised into a worktree.

### 5.2 Design

- **One table, `vault_documents`.**
  - Columns: `workspace_id`, `tree` (`vault` | `skills`), `path` (tree-relative, exactly today's relative file path), `content` (text: the Markdown or JSON as it would be on disk), `content_hash`, `created_at`, `updated_at`.
  - Primary key `(workspace_id, tree, path)`. Row-level security by `workspace_id`, like every tenant table.
- **`PgDocumentStorage(StorageBackend)`** is the second implementation of the existing seam.
  - Writes are upserts.
  - `list_files(subdir, pattern)` becomes a prefix + glob query.
  - A batch `read_many` lets the index rebuild be one `SELECT` instead of N reads.
  - It passes the same contract tests as `FileSystemStorage`.
- **Every vault and skills access goes through the seam.** A note keeps its identity, an absolute `Path` under `vault.root`, so the ~214 `relative_to(root)` / `resolve_path` sites do not change. On rows, the root is only a name. Only the I/O moves: `Vault` gains `read`, `write`, `append`, `exists`, `delete`, `delete_tree`, `move` and a store-backed `read_notes`, and the bypass sites call those (with `delete_prefix` for the bootstrap-retry `rmtree`). An AST guard keeps new `Path` I/O out of the knowledge and skills code, the way the cross-tenant allowlist guards `'*'` reads.
- **Locks become cross-process.** Each `asyncio.Lock` that guards a read-modify-write becomes a transaction-scoped Postgres advisory lock keyed by `(workspace, lock name)`. That fixes the two-writer race on one box as well.
- **Derived state does not change.** `note_embeddings` keeps its `(workspace_id, note_path)` key, and reconcile reads `content_hash` from the row instead of hashing the file.
- **The switch is one setting.**
  - `knowledge_store_backend = filesystem | postgres` (default `filesystem`).
  - `python -m backend.knowledge.store import` loads `var/vault` and `var/skills` into the table. It is idempotent by `content_hash`.
  - `… export <dir>` writes the same tree back out, so Markdown on disk is always one command away.
  - Cut-over: deploy, import, flip the setting, keep the old directory read-only as a backup until a week of clean runs.

### 5.3 What does not change

- The Markdown, the frontmatter, the path layout, and every caller's idea of "a note at a path".
- `note_embeddings`, `ontology_corrections` and `settle_drains`.
- A single box. It gets the same table and loses nothing but the race.

### 5.4 Artifact reads from another machine

`GET /api/v1/deliverables/{id}/artifacts/{ref}` and the step handoff read `var/runs/<rid>/<ref>`. With replicas, the API that answers may not hold the tree. Resolution order:

1. The local run tree, if present.
2. **The run's checkpoint** (§2.2): fetch `refs/bsvibe/checkpoints/<rid>` from the bundle into the product repo, then run `git show <ref>:<path>`. This needs no worktree.
3. The product's main (today's fallback, already R2-aware).

### 5.5 Order of PRs

1. `vault_documents` + RLS + `PgDocumentStorage`, behind the `StorageBackend` contract tests.
2. Move the bypass sites onto the seam, one area per PR, with no behaviour change on `filesystem`:
   - 2a: `Vault` I/O and the garden writer;
   - 2b: one store factory (`backend.knowledge.vault_store`) and the `knowledge_store_backend` setting, with every hand-built store moved onto it;
   - 2c: originals, ingest indexes, the code graph and the ontology;
   - 2d: retrieval, MCP tools, `/inside` and skills;
   - 2e: the AST guard on direct file I/O.
3. Skills through the same seam.
4. Advisory locks in place of the in-process locks.
5. Import/export command, the `knowledge_store_backend` switch, and the cut-over runbook.
6. Artifact reads fall back to the checkpoint (§5.4).

Stage 3 (artifacts beyond the worktree) turned out not to exist.

## 6. Stage 4 — what a second machine still broke

Measured 2026-10-21 (sandbox state, concurrency caps, reapers, other per-process state).

**Fixed:**

- **A run that comes back lost the other machine's work.** A→B→A: A kept its leftover tree because a `.git` was present, and never compared it with the newer checkpoint B saved. Now a tree is kept only when this machine saved the latest checkpoint. Otherwise this machine's copy is discarded and the checkpoint restored. A stale record from another machine is refused even when an older tree is here.
- **Work-tool calls for a run nobody drives ran wherever they landed.** A paused, released or just-finished run has no `home_machine`, so a late call read whatever stale or missing tree the answering replica had, and created a sandbox there. With routing on, such a call is now refused.
- **The sandbox reaper was never started.** `sandbox_reaper_loop` existed and its docstring said it started from the API lifespan; nothing started it. On one box that was masked: the worker re-adopted the box when the run resumed. After a move, a box the API process made for a run stayed up forever, holding a `sandbox_max_concurrent` slot. It now runs in the API process, which is the one that makes boxes for tool calls. It does not run in the worker, where a long LLM turn would lose its box.
- **Verification stacks of two servers shared a compose project.** A client_attach stack runs on the founder's machine, which is one docker for every server. `verify-slot-0` from two servers was one project, so one server's pre-clear tore down the other's live stack. The project now carries the server's machine id. It is unchanged on a single box.

**Checked, unchanged:**

- **Stale-claim lease.** A healthy drive refreshes its claim at every turn boundary, and the lease is twice the turn cap. Two machines drive one run only if one hangs past the lease while still alive.
- **Caps.** Run cap, token budget and per-workspace verify tickets are Postgres counts or locks, so they are global. `sandbox_max_concurrent` and the verify disk bound are per machine (per process for the former), which is right for a per-machine resource.

**Known, not blocking:**

- **Bootstrap cancel** reaches only the replica running the bootstrap task.
- **The OAuth limiter** falls back to per-process buckets when Redis is down.
- **Live events** need Redis to cross replicas.

Prod has Redis.
