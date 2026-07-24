"""Worker process settings — pydantic-settings, env-driven.

All vars use the ``BSVIBE_WORKER_`` prefix (mirrors the backend's ``BSVIBE_``
prefix style). Reads ``.env`` when present. This config belongs to the **client**
worker process the founder runs on their own machine — it is intentionally
separate from the backend's :class:`backend.config.Settings`.
"""

from __future__ import annotations

import socket
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Settings for the BSVibe executor worker process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="BSVIBE_WORKER_",
        extra="ignore",
    )

    # The backend API base URL (e.g. https://api.bsvibe.dev).
    server_url: str = "http://localhost:8400"

    # Worker identity. ``token`` (env: ``BSVIBE_WORKER_TOKEN``) is minted at
    # first-run registration and persisted back to ``.env``. The bearer-token
    # register path (Lift E4) sources the host OAuth credential — either from
    # ``access_token`` (env: ``BSVIBE_WORKER_ACCESS_TOKEN``) when set
    # explicitly, or from the credentials file ``bsvibe login`` writes
    # (``~/.config/bsvibe/credentials.json``). Lift E5 (2026-06-06) removed
    # the legacy ``install_token`` path.
    token: str = ""
    access_token: str = ""
    name: str = socket.gethostname()

    # Polling cadence + batching.
    poll_interval_seconds: float = 5.0
    # Short sleep when already at max_parallel_tasks, waiting for a slot.
    capacity_wait_seconds: float = 1.0
    # Max tasks to request per poll call, regardless of free slots.
    poll_batch_max: int = 5

    # Bounded local concurrency — how many tasks run in parallel.
    max_parallel_tasks: int = 3

    # Root under which the worker creates a fresh, isolated per-task working
    # directory. Empty → the OS default temp location (``tempfile.mkdtemp``).
    # The backend dispatches its own container run path in the task payload, but
    # that absolute path is meaningless on this (remote) machine — the worker
    # always runs each task in a local dir it creates here and removes after.
    workspace_root: str = ""

    # Streaming chunks back to the backend via Redis pub/sub (the same Redis the
    # backend dispatch substrate uses). Empty disables streaming — executors
    # still run and results are still POSTed, the backend just falls back to its
    # DB-row terminal state without incremental chunks.
    redis_url: str = ""

    # ── Lift E17 — long-running ``opencode serve`` daemon ────────────────────
    #
    # The pre-E17 ``opencode run`` per-task subprocess paid heavy startup tax
    # (workspace scan, plugin load, tool registry init, agent runtime spin-up)
    # on every call. Dogfood measured 8 hours wall-clock on a trivial 1-line
    # prompt; the same prompt against a long-running ``opencode serve`` +
    # ``POST /session/{id}/message`` finished in 2.7 seconds. E17 keeps the
    # runtime up across tasks and hits its HTTP surface.
    #
    # The worker spawns serve once at startup (bound to localhost, auto-port
    # by default) and keeps it alive until shutdown. ``OpenCodeExecutor.execute``
    # POSTs against the captured URL instead of launching a subprocess.
    # Lift E31 — default to opencode's ``build`` agent so the executor
    # actually edits files in its sandbox instead of stopping at a
    # description. ``plan`` (the pre-E31 default) made the agent return a
    # fix proposal but never produced code; ``build`` reads + edits + runs
    # bash + tests inside the worker's per-task tempdir. The captured
    # files flow into ``record_result`` (B1) → vault artifact_refs when the
    # adapter is given a ``run_id``. Operators who genuinely want planning
    # only can override via env ``BSVIBE_WORKER_OPENCODE_SERVE_AGENT=plan``.
    opencode_serve_agent: str = "build"
    opencode_serve_host: str = "127.0.0.1"
    opencode_serve_port: int = 0
    opencode_serve_startup_timeout_s: float = 30.0
    opencode_request_timeout_s: float = 600.0

    # opencode keeps its session state in a SQLite store under its XDG data
    # dir (``$XDG_DATA_HOME/opencode`` or ``~/.local/share/opencode``). When
    # the running binary is older than whatever opencode last migrated that
    # store with, the schema carries columns the binary doesn't expect
    # (observed live: ``NOT NULL constraint failed: session_message.seq`` on
    # opencode 1.15.12) and every new session's first message insert 500s.
    # The auto-recovery path quarantines that store + restarts serve. Leave
    # this blank to derive the path from XDG/HOME; set it only when opencode's
    # data dir is relocated (``BSVIBE_WORKER_OPENCODE_DATA_DIR``).
    opencode_data_dir: str = ""

    # Worker-managed Claude OAuth credential file (env:
    # ``BSVIBE_WORKER_CLAUDE_OAUTH_PATH``). The ``claude_code`` executor reads it
    # to refresh + inject ``ANTHROPIC_AUTH_TOKEN`` so a launchd-spawned claude
    # (which can't read the Keychain) authenticates instead of falling back to a
    # stale on-disk token. Blank → ``~/.bsvibe/claude_oauth.json``.
    claude_oauth_path: str = ""

    # The interactive ``claude`` CLI's OWN credential file (env:
    # ``BSVIBE_WORKER_CLAUDE_CLI_CREDENTIALS_PATH``), which the CLI keeps fresh
    # and auto-refreshes. Used ONLY as a last-resort, read-only fallback when the
    # worker's own refresh token is burned — the worker borrows the CLI's live
    # access token per call, never adopting its (single-use) refresh token so the
    # CLI's own login is not clobbered. Blank → ``~/.claude/.credentials.json``.
    claude_cli_credentials_path: str = ""


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
