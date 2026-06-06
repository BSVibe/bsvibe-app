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
    # first-run registration and persisted back to ``.env``. Lift E4 added the
    # bearer-token register path: ``access_token`` (env:
    # ``BSVIBE_WORKER_ACCESS_TOKEN``) sources the host OAuth credential
    # explicitly — when empty the worker falls back to the credentials file
    # ``bsvibe login`` writes (``~/.config/bsvibe/credentials.json``) and
    # finally to the deprecated ``install_token`` path (Lift E5 removes).
    token: str = ""
    access_token: str = ""
    install_token: str = ""
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


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
