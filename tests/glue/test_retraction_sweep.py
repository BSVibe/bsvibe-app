"""The retract queue finally has the background sweep its own code promises.

`RetractionService.apply_pending` is the lazy resolver that writes a queued
retract's `retracted_at` tombstone once the 30s undo window closes. Two places
in the repo describe a sweep that drives it:

* `RetractionService`'s module docstring — "whether it fires from a sweep, a
  follow-up request, or **the next worker tick**"
* `api/v1/inside/retraction.py` — "written when the 30-second undo window
  expires (via `apply_pending` from the next call / **background sweep**)"

and the table even carries an index built for it:

    Index("ix_ontology_corrections_pending", "apply_at")   # "Sweep / lazy-resolve query"

There was no sweep. Measured 2026-08-26: `apply_pending` had exactly ONE
production caller — `backend/mcp/tools/knowledge_tools.py`, on four MCP READ
tools — whose own docstring states it plainly: "The retract queue has no
background sweep; the tombstone is written on the *next call that knows the
workspace*."

So a tombstone landed only if an AGENT happened to read the garden over MCP.
prod's 1,277 retractions are all applied for exactly that reason. A founder
working from the PWA issues a retract and the vault is never stamped — the note
keeps grounding answers until something unrelated happens to run.

This plugs the sweep into the SAME `ScheduleRunnerProtocol` seam the repo
already runs three sweeps on (schedule fire, safe-mode expiry, audit
retention). No new mechanism: a runner + one registration.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.knowledge.application.retraction_sweep import RetractionSweepRunner
from backend.knowledge.infrastructure.ontology_db import Base as OntologyBase
from backend.knowledge.infrastructure.ontology_db import OntologyCorrection

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine(OntologyBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


def _const(writer: _RecordingWriter):
    """The factory is async (rooting a vault is a DB read); tests hand back a
    ready-made writer."""

    async def _factory(_ws: uuid.UUID) -> _RecordingWriter:
        return writer

    return _factory


class _RecordingWriter:
    """Stands in for GardenWriter — records which paths were stamped."""

    def __init__(self) -> None:
        self.stamped: list[str] = []

    async def tombstone_note(self, path: str, **_kw: object) -> None:
        self.stamped.append(path)


async def _queue_retract(
    sf: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    node_ref: str,
    apply_at: datetime,
    applied: bool = False,
    cancelled: bool = False,
) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            OntologyCorrection(
                id=row_id,
                workspace_id=workspace_id,
                actor_id=uuid.uuid4(),
                action="retract",
                node_ref=node_ref,
                reason="cleanup",
                signal_json={},
                issued_at=apply_at - timedelta(seconds=30),
                apply_at=apply_at,
                applied_at=apply_at if applied else None,
                cancelled_at=apply_at if cancelled else None,
            )
        )
        await s.commit()
    return row_id


async def _applied_at(sf: async_sessionmaker[AsyncSession], row_id: uuid.UUID) -> datetime | None:
    async with sf() as s:
        row = (
            await s.execute(select(OntologyCorrection).where(OntologyCorrection.id == row_id))
        ).scalar_one()
        return row.applied_at


async def test_a_past_deadline_retract_is_applied_without_anyone_reading(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """THE point of this lift — nobody called an MCP read tool."""
    ws = uuid.uuid4()
    row_id = await _queue_retract(
        sf,
        workspace_id=ws,
        node_ref="garden/a.md",
        apply_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    writer = _RecordingWriter()

    fired = await RetractionSweepRunner(writer_factory=_const(writer)).fire_due(
        session_factory=sf, now=datetime.now(UTC)
    )

    assert fired == 1
    assert writer.stamped == ["garden/a.md"]
    assert await _applied_at(sf, row_id) is not None


async def test_a_retract_still_inside_its_undo_window_is_untouched(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """The 30s undo window is the whole reason the queue exists. A sweep that
    ignores it would tombstone work the founder is about to take back."""
    ws = uuid.uuid4()
    row_id = await _queue_retract(
        sf,
        workspace_id=ws,
        node_ref="garden/b.md",
        apply_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    writer = _RecordingWriter()

    fired = await RetractionSweepRunner(writer_factory=_const(writer)).fire_due(
        session_factory=sf, now=datetime.now(UTC)
    )

    assert fired == 0
    assert writer.stamped == []
    assert await _applied_at(sf, row_id) is None


async def test_the_sweep_is_system_wide_across_workspaces(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """``apply_pending`` is per-workspace, so a sweep that only knows one
    workspace is the same gap in a new place — it must find them all."""
    a, b = uuid.uuid4(), uuid.uuid4()
    past = datetime.now(UTC) - timedelta(minutes=1)
    await _queue_retract(sf, workspace_id=a, node_ref="garden/a.md", apply_at=past)
    await _queue_retract(sf, workspace_id=b, node_ref="garden/b.md", apply_at=past)
    seen: list[uuid.UUID] = []

    async def _writer_for(ws: uuid.UUID) -> _RecordingWriter:
        seen.append(ws)
        return _RecordingWriter()

    fired = await RetractionSweepRunner(writer_factory=_writer_for).fire_due(
        session_factory=sf, now=datetime.now(UTC)
    )

    assert fired == 2
    assert set(seen) == {a, b}


async def test_a_second_tick_does_not_re_apply(sf: async_sessionmaker[AsyncSession]) -> None:
    """Idempotence — the sweep runs forever; a stamped row must not restamp."""
    ws = uuid.uuid4()
    past = datetime.now(UTC) - timedelta(minutes=1)
    await _queue_retract(sf, workspace_id=ws, node_ref="garden/a.md", apply_at=past)
    writer = _RecordingWriter()
    runner = RetractionSweepRunner(writer_factory=_const(writer))

    first = await runner.fire_due(session_factory=sf, now=datetime.now(UTC))
    second = await runner.fire_due(session_factory=sf, now=datetime.now(UTC))

    assert (first, second) == (1, 0)
    assert writer.stamped == ["garden/a.md"]


async def test_a_cancelled_retract_is_never_applied(sf: async_sessionmaker[AsyncSession]) -> None:
    """POSITIVE CONTROL for Undo — the founder took it back."""
    ws = uuid.uuid4()
    past = datetime.now(UTC) - timedelta(minutes=1)
    await _queue_retract(sf, workspace_id=ws, node_ref="garden/a.md", apply_at=past, cancelled=True)
    writer = _RecordingWriter()

    fired = await RetractionSweepRunner(writer_factory=_const(writer)).fire_due(
        session_factory=sf, now=datetime.now(UTC)
    )

    assert fired == 0
    assert writer.stamped == []


async def test_the_sweep_is_registered_in_the_worker_runtime() -> None:
    """SEAM — a runner nobody schedules is the same silence with more code.
    prod's tombstones only landed because an agent happened to read over MCP."""
    import inspect

    from backend.workflow.application.runtime import worker_runtime

    src = inspect.getsource(worker_runtime.build_worker_runtime)
    assert "RetractionSweepRunner" in src


async def test_the_writer_factory_imports_actually_resolve() -> None:
    """The factory's imports are LAZY (composition root must not pull the vault
    stack at import time), so a wrong module path is invisible until the sweep
    first fires in production — and the registration test above still passes.

    Caught exactly that while writing this: ``backend.knowledge.vault`` does not
    exist; the real module is ``backend.knowledge.graph.vault``. A lazy import is
    a promise the test suite does not otherwise check."""
    import importlib
    import inspect
    import re

    from backend.workflow.application.runtime import worker_runtime

    src = inspect.getsource(worker_runtime._retraction_writer_for)
    imports = re.findall(r"from ([\w.]+) import ([\w, ]+)", src)
    assert imports, "the factory is expected to use lazy imports"
    for module_path, names in imports:
        module = importlib.import_module(module_path)
        for name in (n.strip() for n in names.split(",")):
            assert hasattr(module, name), f"{module_path} has no {name}"
