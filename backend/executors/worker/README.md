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

1. An admin mints an **install token** for the workspace:

   ```
   POST /api/v1/workers/install-token   (admin JWT)  ->  { "token": "..." }
   ```

2. On the worker machine, set the environment (or a `.env` file in the working
   directory) and run the module:

   ```bash
   export BSVIBE_WORKER_SERVER_URL="https://api.bsvibe.dev"
   export BSVIBE_WORKER_INSTALL_TOKEN="<the install token from step 1>"
   # Optional — enable streaming chunks back to the backend:
   # export BSVIBE_WORKER_REDIS_URL="redis://<backend-redis-host>:6379/0"

   python -m backend.executors.worker
   ```

   On the **first** run the worker registers itself with the install token and
   persists the returned **worker token** to `.env` (as `BSVIBE_WORKER_TOKEN`),
   along with its name + server URL. Subsequent runs reuse that token and never
   need the install token again.

## Environment variables

All vars use the `BSVIBE_WORKER_` prefix and may be set via env or a `.env` file.

| Variable | Default | Purpose |
|----------|---------|---------|
| `BSVIBE_WORKER_SERVER_URL` | `http://localhost:8400` | Backend API base URL. |
| `BSVIBE_WORKER_INSTALL_TOKEN` | _(empty)_ | Required only on first run, to register. |
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
└── main.py            # register / handle_task / run_once / poll_and_execute + entrypoint
```
