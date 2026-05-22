"""``python -m backend.workers`` — the production worker daemon.

Runs every DB-polling worker with real dependencies until SIGINT/SIGTERM:
intake → agent (with the gateway work-LLM + sandbox) → delivery (real plugin
dispatcher) → settle (BSage write subscriber) → relay (audit outbox drain).

See :mod:`backend.workers.run` for the wiring; this module is the thin process
shell (logging + ``asyncio.run``).
"""

from __future__ import annotations

import asyncio

from backend.shared.core.logging import configure_logging
from backend.workers.run import run_workers


def main() -> None:
    configure_logging(level="INFO", service_name="bsvibe-workers")
    asyncio.run(run_workers())


if __name__ == "__main__":
    main()
