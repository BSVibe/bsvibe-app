"""BSVibe executor worker — the installable headless client process.

Lift 3 of the executor-pool epic. This package is a **client** the founder
runs on their own machine (where ``claude``/``codex``/``opencode`` are already
logged in). It talks to the backend over HTTP (``/api/v1/workers/*``) and runs
CLIs as subprocesses; it deliberately does **not** import SQLAlchemy or the DB
— the server side (Lifts 1-2) owns persistence.

Run it with::

    python -m backend.executors.worker

See ``README.md`` in this directory for the env vars and operational notes.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — public surface lives in nested modules.
__all__: list[str] = []
