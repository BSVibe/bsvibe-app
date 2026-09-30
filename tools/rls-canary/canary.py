"""Differential RLS canary — run the DB-only background ticks, report row deltas.

Usage: CANARY_APP_URL=... CANARY_OWNER_URL=... uv run python canary.py
Run it against two restores of the same prod snapshot (policy open vs closed)
and compare: the deltas must be identical.
"""

# ruff: noqa: PLC0415, S608 — imports follow the env set in main() (settings read paths at
# import); table names come from pg_tables, not input.
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

APP_URL = os.environ["CANARY_APP_URL"]
OWNER_URL = os.environ["CANARY_OWNER_URL"]
ROUNDS = int(os.environ.get("CANARY_ROUNDS", "3"))


class _StubSink:
    async def absorb(self, settlement: object) -> str:
        return f"garden/canary-{uuid.uuid4().hex}.md"


async def _counts(owner) -> dict[str, int]:  # type: ignore[no-untyped-def]
    async with owner.connect() as conn:
        tables = [
            r[0]
            for r in await conn.execute(
                text("select tablename from pg_tables where schemaname='public' order by 1")
            )
        ]
        out = {}
        for t in tables:
            out[t] = (await conn.execute(text(f'select count(*) from "{t}"'))).scalar_one()
        return out


async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="canary-")
    for key, sub in (
        ("BSVIBE_KNOWLEDGE_VAULT_ROOT", "vault"),
        ("BSVIBE_RUN_WORKSPACE_ROOT", "runs"),
        ("BSVIBE_PRODUCT_WORKSPACE_ROOT", "products"),
    ):
        os.environ[key] = os.path.join(tmp, sub)

    from backend.knowledge.infrastructure.workers.settle_worker import SettleWorker
    from backend.schedule.infrastructure.workers.schedule_worker import ScheduleWorker
    from backend.workflow.application.runtime.worker_runtime import (  # noqa: F401
        build_db_poll_schedule_runner,
    )
    from backend.workflow.infrastructure.workers.agent_worker import AgentWorker
    from backend.workflow.infrastructure.workers.daily_brief_worker import DailyBriefWorker
    from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker
    from plugin.audit.retention_sweep import AuditRetentionSweepRunner

    app = create_async_engine(APP_URL, future=True)
    owner = create_async_engine(OWNER_URL, future=True)
    sf = async_sessionmaker(app, expire_on_commit=False)

    from backend.workflow.application.runtime import worker_runtime as wr

    safe_mode_runner = wr.SafeModeExpirySweepRunner()
    schedule = ScheduleWorker(session_factory=sf, runner=build_db_poll_schedule_runner())
    safe_mode = ScheduleWorker(session_factory=sf, runner=safe_mode_runner, name="safe_mode")
    retention = ScheduleWorker(session_factory=sf, runner=AuditRetentionSweepRunner(), name="ret")
    intake = IntakeWorker(session_factory=sf)
    agent = AgentWorker(session_factory=sf)
    brief = DailyBriefWorker(session_factory=sf)
    settle = SettleWorker(session_factory=sf, sink=_StubSink())

    before = await _counts(owner)
    log: list[dict[str, object]] = []
    for r in range(ROUNDS):
        step: dict[str, object] = {"round": r}
        for name, fn in (
            ("schedule", schedule.fire_due_once),
            ("safe_mode", safe_mode.fire_due_once),
            ("retention", retention.fire_due_once),
            ("intake", intake.drain_once),
            ("answers", agent.apply_queued_answers),
            ("claim", agent.claim_once),
            ("reap_stale", agent._reap_stale_claims),
            ("brief", brief.run_once),
            ("settle", settle.drain_once),
        ):
            try:
                step[name] = await fn()
            except Exception as exc:  # noqa: BLE001
                step[name] = f"ERR {type(exc).__name__}: {str(exc)[:120]}"
        log.append(step)
    after = await _counts(owner)
    delta = {t: after[t] - before[t] for t in after if after[t] != before[t]}
    print(json.dumps({"ticks": log, "delta": delta}, ensure_ascii=False, default=str))
    await app.dispose()
    await owner.dispose()


asyncio.run(main())
