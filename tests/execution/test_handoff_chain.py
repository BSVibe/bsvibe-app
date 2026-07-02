"""P1-L2: design→impl handoff — a verified DESIGN run in a design_then_impl
pipeline spawns an IMPLEMENTATION run seeded with the design run's id + refs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from backend.router.routing.run_routing.db import RunRoutingRuleRow
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from tests._support import memory_session


async def _seed_design_run(
    session: Any,
    *,
    pipeline: str = "design_then_impl",
    stage: str | None = None,
    refs: list[str] | None = None,
    with_rules: bool = True,
) -> ExecutionRun:
    ws = uuid.uuid4()
    if with_rules:
        # The chaining gate requires the workspace to have opted into routing.
        session.add(
            RunRoutingRuleRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                name="impl-stage",
                priority=10,
                is_default=False,
                target="executor/opencode",
                conditions=[{"field": "stage", "operator": "eq", "value": "impl"}],
                is_active=True,
            )
        )
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=ws,
        product_id=None,  # non-product → transition skips auto-ship, still chains
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={
            "intent_text": "build the auth service",
            "stage": stage,
            "frame": {"artifact_type_hint": "code", "pipeline": pipeline},
        },
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.add(run)
    if refs is not None:
        session.add(
            Deliverable(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=ws,
                deliverable_type=DeliverableType.CODE,
                payload={"summary": "design spec", "artifact_refs": refs},
                created_at=datetime.now(tz=UTC),
            )
        )
    await session.flush()
    return run


async def _impl_runs(session: Any, *, exclude: uuid.UUID) -> list[ExecutionRun]:
    rows = (await session.execute(select(ExecutionRun).where(ExecutionRun.id != exclude))).scalars()
    return [r for r in rows if (r.payload or {}).get("stage") == "impl"]


async def test_verified_design_run_spawns_impl_run() -> None:
    async with memory_session() as s:
        design = await _seed_design_run(s, refs=["docs/spec.md", "docs/api.md"])
        runner = AgentRunner(s)

        await runner.transition(run_id=design.id, to_status=RunStatus.REVIEW_READY)

        impls = await _impl_runs(s, exclude=design.id)
        assert len(impls) == 1
        impl = impls[0]
        assert impl.status is RunStatus.OPEN
        assert impl.workspace_id == design.workspace_id
        assert impl.request_id == design.request_id
        assert impl.payload["stage"] == "impl"
        assert impl.payload["pipeline"] == "design_then_impl"
        assert impl.payload["design_run_id"] == str(design.id)
        assert impl.payload["design_artifact_refs"] == ["docs/spec.md", "docs/api.md"]
        # The design intent carries over so the impl frame stays coherent.
        assert impl.payload["intent_text"] == "build the auth service"


async def test_impl_stage_run_does_not_respawn() -> None:
    """The impl run must NOT spawn another impl run — the chain is exactly two."""
    async with memory_session() as s:
        impl_design = await _seed_design_run(s, stage="impl", refs=["x.py"])
        runner = AgentRunner(s)

        await runner.transition(run_id=impl_design.id, to_status=RunStatus.REVIEW_READY)

        assert await _impl_runs(s, exclude=impl_design.id) == []


async def test_single_pipeline_run_does_not_spawn() -> None:
    async with memory_session() as s:
        run = await _seed_design_run(s, pipeline="single", refs=["x.py"])
        runner = AgentRunner(s)

        await runner.transition(run_id=run.id, to_status=RunStatus.REVIEW_READY)

        assert await _impl_runs(s, exclude=run.id) == []


async def test_spawn_with_no_deliverable_yields_empty_refs() -> None:
    async with memory_session() as s:
        design = await _seed_design_run(s, refs=None)  # no deliverable rows
        runner = AgentRunner(s)

        await runner.transition(run_id=design.id, to_status=RunStatus.REVIEW_READY)

        impls = await _impl_runs(s, exclude=design.id)
        assert len(impls) == 1
        assert impls[0].payload["design_artifact_refs"] == []


async def test_no_chaining_without_routing_rules() -> None:
    """Gate: a rule-less workspace keeps single-run behaviour — chaining a
    design→impl pair onto one model would just run the work twice."""
    async with memory_session() as s:
        design = await _seed_design_run(s, refs=["spec.md"], with_rules=False)
        runner = AgentRunner(s)

        await runner.transition(run_id=design.id, to_status=RunStatus.REVIEW_READY)

        assert await _impl_runs(s, exclude=design.id) == []


async def test_spawn_inlines_design_spec_text(tmp_path: Any) -> None:
    """D-2: at spawn (design worktree present) the spec TEXT is captured inline
    on the impl payload — durable even if the worktree is later cleaned up or the
    design run was held and never shipped to main."""
    from pathlib import Path
    from types import SimpleNamespace

    from backend.storage.artifact_store import LocalFilesystemArtifactStore

    settings = SimpleNamespace(
        product_workspace_root=str(tmp_path / "products"),
        run_workspace_root=str(tmp_path / "runs"),
    )
    async with memory_session() as s:
        design = await _seed_design_run(s, refs=["docs/spec.md"])
        LocalFilesystemArtifactStore(Path(settings.run_workspace_root)).put(
            design.id, "docs/spec.md", b"# Spec\nBuild the auth service.\n"
        )
        runner = AgentRunner(s, settings=settings)  # type: ignore[arg-type]

        await runner.transition(run_id=design.id, to_status=RunStatus.REVIEW_READY)

        impl = (await _impl_runs(s, exclude=design.id))[0]
        spec = impl.payload["design_spec_text"]
        assert spec is not None
        assert "Build the auth service." in spec


async def test_spawn_inlines_none_when_spec_unreadable(tmp_path: Any) -> None:
    """Refs present but no readable file → design_spec_text is None (honest
    has_spec=false), refs still recorded for provenance. The impl run proceeds."""
    from types import SimpleNamespace

    settings = SimpleNamespace(
        product_workspace_root=str(tmp_path / "products"),
        run_workspace_root=str(tmp_path / "runs"),
    )
    async with memory_session() as s:
        design = await _seed_design_run(s, refs=["docs/spec.md"])  # no file on disk
        runner = AgentRunner(s, settings=settings)  # type: ignore[arg-type]

        await runner.transition(run_id=design.id, to_status=RunStatus.REVIEW_READY)

        impl = (await _impl_runs(s, exclude=design.id))[0]
        assert impl.payload["design_spec_text"] is None
        assert impl.payload["design_artifact_refs"] == ["docs/spec.md"]
