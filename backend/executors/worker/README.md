# BSVibe Executor Worker

The **installable worker process** — a headless client the founder runs on
their own machine (e.g. the Mac Mini), where `claude` (and later `codex` /
`opencode`) are **already logged in**. It polls the BSVibe backend for executor
tasks, runs them through the local CLI as a subprocess, streams the output back,
and reports the result.

It is a **client** process: it talks to the backend purely over HTTP
(`/api/v1/workers/*`) and runs CLIs as subprocesses. It does **not** import the
database or any server-side module — it is dependency-light on purpose.

## What it does

On startup it registers itself (first run only), then loops:

```
heartbeat  ->  poll(count = free slots)  ->  for each task:
    select executor by executor_type  ->  run it (subprocess)  ->  collect output
    ->  [optionally publish stream chunks to task:{id}:stream via Redis]
    ->  POST /api/v1/workers/result  ->  [publish task:{id}:done]
```

* **Bounded concurrency** — at most `max_parallel_tasks` run at once.
* **Graceful shutdown** — SIGINT / SIGTERM stop the loop and let in-flight tasks
  drain (or cancel) before exiting.
* **Streaming is optional** — set `BSVIBE_WORKER_REDIS_URL` to publish
  incremental output chunks to the backend's `task:{id}:stream` / `:done`
  pub/sub channels; without it the worker still runs and still POSTs results, the
  backend just falls back to the DB row's terminal state.

This lift ships only the **`claude_code`** executor (runs
`claude --print --output-format stream-json`). `codex` / `opencode` are
*detected* (so the worker registers the right capabilities) but their executors
are a follow-up lift.

## Running it (on the Mac Mini)

The host must already have `claude` installed and logged in — the worker reuses
the host's existing Claude login and harness (`CLAUDE.md`, `settings.json`,
hooks). No API key is passed by the backend.

The GitHub-Actions-runner UX:

```bash
bsvibe login                          # PKCE loopback OAuth on this host
bsvibe-worker register --name $(hostname)
bsvibe-worker run
```

`bsvibe login` writes the host OAuth credential to
`~/.config/bsvibe/credentials.json`. `bsvibe-worker register` sends that
credential as `Authorization: Bearer` to `POST /api/v1/workers/register`; the
backend derives the workspace from the verified claims and returns a fresh
per-worker token, which the CLI persists to `~/.bsvibe/worker.token` (and
`.env` as `BSVIBE_WORKER_TOKEN`). Subsequent runs of `bsvibe-worker run`
reuse that token.

Optional streaming back-channel:

```bash
export BSVIBE_WORKER_REDIS_URL="redis://<backend-redis-host>:6379/0"
```

## Environment variables

All vars use the `BSVIBE_WORKER_` prefix and may be set via env or a `.env` file.

| Variable | Default | Purpose |
|----------|---------|---------|
| `BSVIBE_WORKER_SERVER_URL` | `http://localhost:8400` | Backend API base URL. |
| `BSVIBE_WORKER_ACCESS_TOKEN` | _(empty)_ | Optional explicit OAuth bearer; falls back to `~/.config/bsvibe/credentials.json` when empty. |
| `BSVIBE_WORKER_TOKEN` | _(empty)_ | Per-worker token; written to `.env` after first registration. |
| `BSVIBE_WORKER_NAME` | hostname | The worker's display name. |
| `BSVIBE_WORKER_REDIS_URL` | _(empty)_ | Enables streaming output chunks; empty disables streaming. |
| `BSVIBE_WORKER_POLL_INTERVAL_SECONDS` | `5` | Idle poll cadence. |
| `BSVIBE_WORKER_POLL_BATCH_MAX` | `5` | Max tasks requested per poll. |
| `BSVIBE_WORKER_MAX_PARALLEL_TASKS` | `3` | Max tasks running concurrently. |
| `BSVIBE_WORKER_CAPACITY_WAIT_SECONDS` | `1` | Wait when at capacity. |

## Layout

```
backend/executors/worker/
├── __init__.py
├── __main__.py        # `python -m backend.executors.worker`
├── config.py          # WorkerSettings (pydantic-settings, BSVIBE_WORKER_ prefix)
├── executors.py       # ExecutorProtocol, ExecutionChunk/Result, collect(),
│                       # detect_capabilities(), select_executor()
├── claude_code.py     # ClaudeCodeExecutor — the claude_code subprocess streamer
├── main.py            # register / handle_task / run_once / poll_and_execute + entrypoint
├── launchd/           # macOS LaunchAgent template (per-user, auto-restart)
│   └── com.bsvibe.worker.plist.example
└── systemd/           # Linux user-service template (per-user, auto-restart)
    └── bsvibe-worker.service.example
```

## Running it as a persistent service (B14)

Running `python -m backend.executors.worker` directly in a terminal is fine for
dev but disappears the moment that terminal closes. For the founder's Mac Mini
(or any always-on host) install the worker as an OS-managed service so it
auto-starts at login and auto-restarts on crash — with visible logs.

### macOS (Mac Mini) — launchd

Template: [`launchd/com.bsvibe.worker.plist.example`](launchd/com.bsvibe.worker.plist.example).
Edit the `{USER}` / `{REPO}` / `{SERVER_URL}` / `{REDIS_URL}` placeholders,
copy it to `~/Library/LaunchAgents/com.bsvibe.worker.plist`, then:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.bsvibe.worker.plist
launchctl kickstart -k gui/$(id -u)/com.bsvibe.worker
# Logs:
tail -f ~/Library/Logs/bsvibe-worker.out.log ~/Library/Logs/bsvibe-worker.err.log
```

### Linux — systemd (user service)

Template: [`systemd/bsvibe-worker.service.example`](systemd/bsvibe-worker.service.example).
Edit the placeholders, copy it to `~/.config/systemd/user/bsvibe-worker.service`,
then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now bsvibe-worker.service
# Logs:
journalctl --user -u bsvibe-worker -f
```

## Verifying the worker is alive

Two ways to confirm the worker is heart-beating:

1. **PWA Models tab → Executor Workers** — surfaces the worker's name,
   `status` (`online` / `offline`), and `last_heartbeat` timestamp from
   `GET /api/v1/workers`. Refresh after starting the service; it should flip to
   `online` within the heartbeat interval (≈30s) and tick `last_heartbeat`
   forward on every poll.
2. **Backend startup log** — when executor `ModelAccount`s are active but the
   backend has no `BSVIBE_REDIS_URL` configured (no transport for dispatch),
   `run_workers` emits a single structured WARNING at startup:

   ```
   executor_dispatch_no_redis  executor_account_count=N  hint=…
   ```

   This is the loud-at-startup guard for the configure-once-forget mistake;
   the runtime keeps booting but every executor run would otherwise raise a
   `no_executor_dispatch_transport` Decision after the fact.
