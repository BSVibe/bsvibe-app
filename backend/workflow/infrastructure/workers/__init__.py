"""Workflow context — infrastructure workers.

Per v8 D34, workers belong in ``<context>/infrastructure/workers/``. The
agent / intake / delivery / verifier / relay workers + the production
runtime (``run.py``) all bind Workflow domain + application logic to the
DB-poll / Redis-Streams trigger substrate.

The settle worker (BSage write subscriber) belongs to the Knowledge
context — see :mod:`backend.knowledge.infrastructure.workers.settle_worker`.
The schedule runner stays at :mod:`backend.workers.schedule_runner` pending
the Schedule context lift. Common infra (``base``, ``db``, ``emit``,
``streams``, ``relays``) is shared across contexts and remains at
:mod:`backend.workers`.
"""

from __future__ import annotations
