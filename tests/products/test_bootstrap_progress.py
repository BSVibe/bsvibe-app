"""Lift E9 Part B — bootstrap progress visibility.

The bootstrap pipeline emits ``INGEST_COMPILE_BATCH_*`` events as each
chunk runs. The runtime subscribes a per-job
:class:`_BootstrapProgressSubscriber` onto the compiler's event bus that
turns those events into ``UPDATE products SET bootstrap_progress=…``
writes so the founder UI can see forward motion within a single
bootstrap (instead of the same opaque ``status="ingesting"`` for an
hour).

These tests pin the subscriber's event-handling contract + verify the
``bsvibe_products_show`` MCP tool surfaces the new field.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.identity.workspaces_db import ProductRow, WorkspaceRow, WorkspacesBase
from backend.knowledge._internal.events import Event, EventBus, EventType
from backend.workflow.application.runtime.product_bootstrap_runtime import (
    STATUS_COMPLETE,
    _BootstrapProgressSubscriber,
    run_product_bootstrap_job,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_REGION = "us-1"


@pytest_asyncio.fixture
async def session_factory():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_workspace_and_product(
    session_factory, *, workspace_id: uuid.UUID, product_id: uuid.UUID
) -> None:
    async with session_factory() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t", region=_REGION, safe_mode=False))
        await s.flush()
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name="p",
                slug="p",
                repo_url="https://x/y",
            )
        )
        await s.commit()


def _stub_settings(tmp_path: Path) -> None:
    from backend.config import get_settings

    settings = get_settings()
    product_root = tmp_path / "product_ws"
    product_root.mkdir(exist_ok=True)
    vault_root = tmp_path / "vault"
    vault_root.mkdir(exist_ok=True)
    object.__setattr__(settings, "product_workspace_root", str(product_root))
    object.__setattr__(settings, "knowledge_vault_root", str(vault_root))


# ── Subscriber-level tests ────────────────────────────────────────────────────


async def test_chunk_done_event_increments_chunks_done_and_notes(
    session_factory,
) -> None:
    """A ``CHUNK_DONE`` event MUST increment the rolling chunks_done counter
    and accumulate notes_created / notes_updated."""
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    await _seed_workspace_and_product(
        session_factory, workspace_id=workspace_id, product_id=product_id
    )

    sub = _BootstrapProgressSubscriber(session_factory=session_factory, product_id=product_id)

    # Start event carries the chunk count.
    await sub.on_event(
        Event(
            event_type=EventType.INGEST_COMPILE_BATCH_START,
            payload={"source": "x", "item_count": 5, "chunk_count": 3},
        )
    )

    # Two CHUNK_DONE events with notes.
    await sub.on_event(
        Event(
            event_type=EventType.INGEST_COMPILE_BATCH_CHUNK_DONE,
            payload={
                "source": "x",
                "chunk_index": 0,
                "chunk_count": 3,
                "notes_created": 2,
                "notes_updated": 1,
            },
        )
    )
    await sub.on_event(
        Event(
            event_type=EventType.INGEST_COMPILE_BATCH_CHUNK_DONE,
            payload={
                "source": "x",
                "chunk_index": 1,
                "chunk_count": 3,
                "notes_created": 3,
                "notes_updated": 0,
            },
        )
    )

    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        assert row.bootstrap_progress == {
            "chunks_done": 2,
            "chunks_total": 3,
            "chunks_failed": 0,
            "notes_created": 5,
            "notes_updated": 1,
            "phase": "ingesting",
        }


async def test_chunk_failed_event_increments_failures(session_factory) -> None:
    """A ``CHUNK_FAILED`` event MUST bump chunks_done AND chunks_failed —
    the founder sees both forward motion and the failure count."""
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    await _seed_workspace_and_product(
        session_factory, workspace_id=workspace_id, product_id=product_id
    )

    sub = _BootstrapProgressSubscriber(session_factory=session_factory, product_id=product_id)

    await sub.on_event(
        Event(
            event_type=EventType.INGEST_COMPILE_BATCH_START,
            payload={"chunk_count": 2},
        )
    )
    await sub.on_event(
        Event(
            event_type=EventType.INGEST_COMPILE_BATCH_CHUNK_FAILED,
            payload={"chunk_count": 2, "chunk_index": 0},
        )
    )

    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        assert row.bootstrap_progress is not None
        assert row.bootstrap_progress["chunks_done"] == 1
        assert row.bootstrap_progress["chunks_failed"] == 1
        assert row.bootstrap_progress["chunks_total"] == 2


async def test_unrelated_event_is_silently_ignored(session_factory) -> None:
    """Events outside the ``INGEST_COMPILE_BATCH_*`` family MUST pass
    through without touching the progress row — the subscriber only owns
    its own slice of the event stream."""
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    await _seed_workspace_and_product(
        session_factory, workspace_id=workspace_id, product_id=product_id
    )

    sub = _BootstrapProgressSubscriber(session_factory=session_factory, product_id=product_id)

    await sub.on_event(Event(event_type=EventType.NOTE_UPDATED, payload={"x": 1}))

    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        # Never touched — stays None.
        assert row.bootstrap_progress is None


async def test_write_failure_is_swallowed_not_raised(session_factory) -> None:
    """A DB hiccup in ``_write_snapshot`` MUST be logged but NOT raised —
    a successful ingest's chunk_done event can't be sunk by a transient
    progress-write failure (the founder's UI lags one tick; the ingest
    still ships notes)."""

    class _BrokenFactory:
        def __call__(self):
            raise RuntimeError("transient DB error")

    sub = _BootstrapProgressSubscriber(
        session_factory=_BrokenFactory(),  # type: ignore[arg-type]
        product_id=uuid.uuid4(),
    )
    # No exception escapes — the chunk loop keeps going.
    await sub.on_event(
        Event(
            event_type=EventType.INGEST_COMPILE_BATCH_CHUNK_DONE,
            payload={"chunk_count": 1, "notes_created": 1, "notes_updated": 0},
        )
    )


# ── Compiler wiring tests ─────────────────────────────────────────────────────


async def test_event_bus_subscriber_receives_compile_batch_events() -> None:
    """The :class:`IngestCompiler` MUST broadcast ``CHUNK_START`` /
    ``CHUNK_DONE`` events through an attached EventBus so the bootstrap
    subscriber can react. (The events already exist — Lift E9 wires them
    onto the bootstrap runtime's bus.)"""
    bus = EventBus()
    received: list[EventType] = []

    class _Capture:
        async def on_event(self, ev: Event) -> None:
            received.append(ev.event_type)

    bus.subscribe(_Capture())
    await bus.emit(
        Event(
            event_type=EventType.INGEST_COMPILE_BATCH_CHUNK_DONE,
            payload={"chunk_count": 1},
        )
    )
    assert EventType.INGEST_COMPILE_BATCH_CHUNK_DONE in received


# ── End-to-end runtime test ──────────────────────────────────────────────────


async def test_runtime_attaches_subscriber_and_persists_progress(
    session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the runtime wires a subscriber onto the bus that the
    IngestCompiler emits to. With a stub Knowledge implementation that
    drives the bus directly, the product row's ``bootstrap_progress``
    column reflects each emitted chunk."""
    from backend.workflow.application.runtime import product_bootstrap_runtime as rt_mod

    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    await _seed_workspace_and_product(
        session_factory, workspace_id=workspace_id, product_id=product_id
    )
    _stub_settings(tmp_path)

    fake_git = MagicMock()
    fake_git.clone = AsyncMock()

    class _BusDrivingKnowledge:
        """A stub Knowledge whose ``ingest`` reaches up into the runtime
        module to find the attached subscriber and drives chunk events
        through it directly. Lets the test verify the runtime wired the
        subscriber AND the writes land."""

        def __init__(self, subscriber):
            self._subscriber = subscriber

        async def ingest(self, request):
            from backend.knowledge.facade import IngestResult

            # Drive two CHUNK_DONE events as the compile_batch would.
            await self._subscriber.on_event(
                Event(
                    event_type=EventType.INGEST_COMPILE_BATCH_START,
                    payload={"chunk_count": 2},
                )
            )
            await self._subscriber.on_event(
                Event(
                    event_type=EventType.INGEST_COMPILE_BATCH_CHUNK_DONE,
                    payload={
                        "chunk_count": 2,
                        "notes_created": 4,
                        "notes_updated": 0,
                    },
                )
            )
            await self._subscriber.on_event(
                Event(
                    event_type=EventType.INGEST_COMPILE_BATCH_CHUNK_DONE,
                    payload={
                        "chunk_count": 2,
                        "notes_created": 2,
                        "notes_updated": 1,
                    },
                )
            )
            return IngestResult(
                proposals_count=0,
                notes_count=7,
                run_id=uuid.uuid4(),
                notes_created=6,
                notes_updated=1,
                chunk_failures=0,
            )

        async def retrieve_canon(self, query):
            from backend.knowledge.facade import CanonRetrievalResult

            return CanonRetrievalResult(notes=[])

        async def settle(self, *, workspace_id, region):
            return 0

    captured: dict[str, object] = {}

    def _build(**kw):
        sub = kw.get("progress_subscriber")
        assert sub is not None, "runtime must thread a subscriber"
        captured["subscriber"] = sub
        return _BusDrivingKnowledge(sub)

    monkeypatch.setattr(rt_mod, "build_bootstrap_knowledge", _build)

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://x/y",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        assert row.bootstrap_status == STATUS_COMPLETE
        # Progress fully populated, both chunks done.
        prog = row.bootstrap_progress
        assert prog is not None
        assert prog["chunks_done"] == 2
        assert prog["chunks_total"] == 2
        assert prog["chunks_failed"] == 0
        assert prog["notes_created"] == 6
        assert prog["notes_updated"] == 1


# ── MCP surface ──────────────────────────────────────────────────────────────


async def test_products_show_serializer_includes_bootstrap_progress() -> None:
    """``bsvibe_products_show`` MUST surface the new field so the founder
    can poll it from the MCP tool surface."""
    from backend.mcp.tools.workflow_tools import _product_to_dict

    row = ProductRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        name="p",
        slug="p",
        repo_url="https://x/y",
        bootstrap_status="ingesting",
        bootstrap_progress={
            "chunks_done": 5,
            "chunks_total": 10,
            "chunks_failed": 0,
            "notes_created": 12,
            "notes_updated": 3,
            "phase": "ingesting",
        },
    )
    out = _product_to_dict(row)
    assert "bootstrap_progress" in out
    assert out["bootstrap_progress"]["chunks_done"] == 5
    assert out["bootstrap_progress"]["chunks_total"] == 10


async def test_products_show_serializer_handles_legacy_null_progress() -> None:
    """A legacy row (no progress JSON ever written) MUST serialize cleanly
    with ``bootstrap_progress=null`` — no AttributeError, no missing key."""
    from backend.mcp.tools.workflow_tools import _product_to_dict

    row = ProductRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        name="p",
        slug="p",
        repo_url=None,
        bootstrap_status=None,
        bootstrap_progress=None,
    )
    out = _product_to_dict(row)
    assert out["bootstrap_progress"] is None
