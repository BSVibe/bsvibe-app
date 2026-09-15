"""#959 — the background drive publishes its workspace; the claim stays global.

The request path has two isolation layers plus a structural CI guard
(``tests/production/test_scope_audit.py``). The background paths had ZERO: no
code in ``workflow/`` / ``schedule/`` / ``workers/`` ever set the scoping
contextvar or the RLS GUC, so every query a drive made ran unfiltered at both
layers. Public signup puts other people's data in that same queue.

The claim itself must stay workspace-blind — a queue poller crosses tenants by
design, and ``execution_runs`` is RLS-FORCED, so a scoped claim would stop the
pipeline for everyone. The line between the two is the claim's RETURNING: blind
up to it, scoped after it. Both halves are asserted here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.data import Base
from backend.data.scoping import current_workspace_id
from backend.extensions.skill.loader import SkillLoader
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.agent_loop import LoopTurn, RunOrchestrator
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.workers.agent_worker import (
    AgentExecutionDeps,
    AgentWorker,
)

from .._support import pg_url, use_real_pg

pytestmark = pytest.mark.asyncio


def _deps(root: Path, orchestrator_factory) -> AgentExecutionDeps:  # noqa: ANN001
    def _skill_loader_for(ws_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(root / "skills" / str(ws_id))
        loader.load_all()
        return loader

    return AgentExecutionDeps(
        skill_loader_for=_skill_loader_for,
        orchestrator_factory=orchestrator_factory,
        workspace_root=root,
    )


async def _seed_workspace(sf: async_sessionmaker, ws_id: uuid.UUID) -> None:
    async with sf() as s:
        s.add(
            WorkspaceRow(
                id=ws_id,
                name=f"ws-{ws_id.hex[:6]}",
                safe_mode=False,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()


async def _seed_run(sf: async_sessionmaker, *, ws_id: uuid.UUID) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws_id,
                request_id=uuid.uuid4(),
                status=RunStatus.OPEN,
                payload={"frame": {"skill_match": None}},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    # request_id=None + a pre-seeded frame → framing is skipped (no frame LLM).
    async with sf() as s:
        await s.execute(
            update(ExecutionRun).where(ExecutionRun.id == run_id).values(request_id=None)
        )
        await s.commit()
    return run_id


class _QuietLlm:
    """Ends the loop on the first turn — the drive is the subject, not the agent."""

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        return LoopTurn(content="done", tool_calls=())


async def test_drive_publishes_the_runs_workspace_and_releases_it(tmp_path: Path) -> None:
    """Inside the drive the contextvar carries the run's workspace; after, nothing.

    Recording happens inside the orchestrator factory — i.e. in the drive's own
    session, the place every query of the run is made from.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
        await _seed_workspace(sf, ws_a)
        await _seed_workspace(sf, ws_b)
        run_a = await _seed_run(sf, ws_id=ws_a)
        run_b = await _seed_run(sf, ws_id=ws_b)

        seen: dict[uuid.UUID, uuid.UUID | None] = {}

        def _factory(session, run):  # noqa: ANN001, ANN202
            seen[run.id] = current_workspace_id.get()
            return RunOrchestrator(
                session=session, llm=_QuietLlm(), sandbox_manager=NoopSandboxManager()
            )

        worker = AgentWorker(session_factory=sf, execution=_deps(tmp_path, _factory))
        await worker.drive_once()

        # The claim stayed global: BOTH workspaces' runs were picked up.
        assert set(seen) == {run_a, run_b}, f"claim is no longer cross-tenant: {seen}"
        assert seen[run_a] == ws_a, f"drive ran unscoped / mis-scoped: {seen[run_a]}"
        assert seen[run_b] == ws_b, f"drive ran unscoped / mis-scoped: {seen[run_b]}"
        # And the poller is workspace-blind again once the batch is done.
        assert current_workspace_id.get() is None
    finally:
        await engine.dispose()


async def test_a_crashing_drive_does_not_leak_its_workspace_into_the_next_claim(
    tmp_path: Path,
) -> None:
    """The failure path is the one that matters: a leaked contextvar would make
    the next claim see only the crashed run's workspace."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        ws = uuid.uuid4()
        await _seed_workspace(sf, ws)
        await _seed_run(sf, ws_id=ws)

        def _factory(session, run):  # noqa: ANN001, ANN202
            raise RuntimeError("drive blew up")

        worker = AgentWorker(session_factory=sf, execution=_deps(tmp_path, _factory))
        await worker.drive_once()  # the worker absorbs one run's crash by design
        assert current_workspace_id.get() is None
    finally:
        await engine.dispose()


async def test_claim_query_itself_runs_workspace_blind(tmp_path: Path) -> None:
    """Positive control for the load: ``execution_runs`` is RLS-FORCED and the
    claim has no workspace predicate, so the claim must never inherit a scope.

    Driven on real PG when available — that is where a residual GUC would turn
    the claim fail-CLOSED and quietly stop every other workspace's runs.
    """
    if not use_real_pg():
        pytest.skip("real Postgres required — the fail-closed hazard is a PG/RLS one")

    engine = create_async_engine(pg_url(), future=True, pool_size=1, max_overflow=0)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
        await _seed_workspace(sf, ws_a)
        await _seed_workspace(sf, ws_b)
        run_a = await _seed_run(sf, ws_id=ws_a)

        driven: list[uuid.UUID] = []

        def _factory(session, run):  # noqa: ANN001, ANN202
            driven.append(run.id)
            return RunOrchestrator(
                session=session, llm=_QuietLlm(), sandbox_manager=NoopSandboxManager()
            )

        worker = AgentWorker(session_factory=sf, execution=_deps(tmp_path, _factory))
        await worker.drive_once()
        assert run_a in driven

        # A second batch, on the SAME pooled connection the scoped drive used.
        run_b = await _seed_run(sf, ws_id=ws_b)
        await worker.drive_once()
        assert run_b in driven, "the claim went blind to another workspace after a scoped drive"
    finally:
        await engine.dispose()
