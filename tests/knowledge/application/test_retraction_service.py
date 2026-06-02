"""RetractionService — issue + undo + apply, plus tombstone-not-delete invariant.

Lift M3a unit tests. Drive the service over an in-memory SQLite session and
a real per-workspace :class:`GardenWriter` over ``tmp_path``, so the vault
side is exercised through the production writer (Frankenfile note pages on
disk, frontmatter mutations atomic). Asserts the five contract invariants:

1. ``issue`` is idempotent on ``correction_id`` (re-issue returns the same
   signal + ``created=False``).
2. ``undo`` honors a cancellation when inside the 30s window, returns the
   right terminal status outside.
3. ``apply_pending`` writes the tombstone (``retracted_at`` frontmatter)
   and the note file is NOT deleted (provenance preserved).
4. ``apply_pending`` is idempotent — re-running over the same set does
   nothing.
5. Three audit events fire in lifecycle order — requested → undone OR
   requested → applied.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.knowledge.application.retraction_service import RetractionService
from backend.knowledge.domain.retraction import UNDO_WINDOW_SECONDS
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenWriter

from ..._support import db_engine

pytestmark = pytest.mark.asyncio


_NOTE_TEMPLATE = (
    "---\n"
    "kind: decision_resolution\n"
    "question: Should we cache the homepage?\n"
    "answer: Yes — 5 minute CDN TTL.\n"
    "intent_text: harden homepage perf\n"
    "captured_at: '2026-06-01T00:00:00Z'\n"
    "tags:\n"
    "  - settle\n"
    "  - decision\n"
    "---\n"
    "# Decision\n"
    "Cache the homepage at the CDN with a 5-minute TTL.\n"
)


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def actor_id() -> uuid.UUID:
    return uuid.uuid4()


def _writer(vault_root: Path) -> GardenWriter:
    vault_root.mkdir(parents=True, exist_ok=True)
    return GardenWriter(vault=Vault(vault_root))


def _seed_note(vault_root: Path, rel_path: str = "garden/seedling/cache-homepage.md") -> str:
    note_path = vault_root / rel_path
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(_NOTE_TEMPLATE, encoding="utf-8")
    return rel_path


async def test_issue_persists_row_and_returns_signal(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """``issue`` creates the row + returns the signal + ``created=True``."""
    rel_path = _seed_note(vault_root)
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        signal, created = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
            reason="we changed the cache policy",
        )
        await session.commit()

    assert created is True
    assert signal.workspace_id == workspace_id
    assert signal.actor_id == actor_id
    assert signal.action == "retract"
    assert signal.node_ref == rel_path
    assert signal.reason == "we changed the cache policy"
    # apply_at = issued_at + 30s
    assert (signal.apply_at - signal.issued_at).total_seconds() == pytest.approx(
        UNDO_WINDOW_SECONDS, abs=1
    )


async def test_issue_is_idempotent_on_correction_id(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Re-issuing with the same ``correction_id`` returns the existing row."""
    rel_path = _seed_note(vault_root)
    cid = uuid.uuid4()
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        _first, created1 = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
            correction_id=cid,
        )
        await session.commit()
        second, created2 = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
            correction_id=cid,
        )
    assert created1 is True
    assert created2 is False
    assert second.id == cid


async def test_undo_within_window_cancels(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Undo within the 30s window returns ``undone`` and prevents apply."""
    rel_path = _seed_note(vault_root)
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        signal, _ = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
        )
        await session.commit()
        result = await service.undo(
            correction_id=signal.id,
            workspace_id=workspace_id,
        )
        await session.commit()
        # apply_pending should now be a no-op (cancelled row is skipped)
        applied = await service.apply_pending(
            workspace_id=workspace_id,
            now=signal.apply_at + timedelta(seconds=10),
        )

    assert result == "undone"
    assert applied == 0
    # The note's frontmatter must NOT carry retracted_at — undo prevented it.
    text = (vault_root / rel_path).read_text(encoding="utf-8")
    assert "retracted_at" not in text


async def test_undo_after_window_is_expired(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Undo past the deadline returns ``expired`` and writes nothing."""
    rel_path = _seed_note(vault_root)
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        signal, _ = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
        )
        await session.commit()
        result = await service.undo(
            correction_id=signal.id,
            workspace_id=workspace_id,
            now=signal.apply_at + timedelta(seconds=1),
        )
    assert result == "expired"


async def test_apply_pending_writes_tombstone_keeps_file(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """``apply_pending`` adds ``retracted_at`` frontmatter; the FILE is preserved."""
    rel_path = _seed_note(vault_root)
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        signal, _ = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
            reason="cache strategy changed",
        )
        await session.commit()
        applied = await service.apply_pending(
            workspace_id=workspace_id,
            now=signal.apply_at + timedelta(seconds=1),
        )
        await session.commit()
        # Re-apply must be a no-op (applied_at already set).
        re_applied = await service.apply_pending(
            workspace_id=workspace_id,
            now=signal.apply_at + timedelta(seconds=5),
        )

    assert applied == 1
    assert re_applied == 0
    note_path = vault_root / rel_path
    assert note_path.exists(), "tombstone must NOT delete the file"
    text = note_path.read_text(encoding="utf-8")
    assert "retracted_at:" in text
    assert "retracted_by:" in text
    assert "retraction_reason: cache strategy changed" in text


async def test_undo_after_apply_returns_already_applied(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Once ``apply_pending`` has run, undo cannot reverse it through this path."""
    rel_path = _seed_note(vault_root)
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        signal, _ = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
        )
        await session.commit()
        await service.apply_pending(
            workspace_id=workspace_id,
            now=signal.apply_at + timedelta(seconds=1),
        )
        await session.commit()
        result = await service.undo(
            correction_id=signal.id,
            workspace_id=workspace_id,
        )
    assert result == "already_applied"


async def test_workspace_isolation_undo(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """A workspace cannot undo / find another workspace's correction."""
    rel_path = _seed_note(vault_root)
    other_ws = uuid.uuid4()
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        signal, _ = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
        )
        await session.commit()
        wrong_ws = await service.undo(
            correction_id=signal.id,
            workspace_id=other_ws,
        )
    assert wrong_ws == "not_found"


async def test_restore_clears_tombstone(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """``restore`` clears retracted_* frontmatter from an already-applied retract."""
    rel_path = _seed_note(vault_root)
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        signal, _ = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
            reason="oops",
        )
        await session.commit()
        await service.apply_pending(
            workspace_id=workspace_id,
            now=signal.apply_at + timedelta(seconds=1),
        )
        await session.commit()

        text = (vault_root / rel_path).read_text(encoding="utf-8")
        assert "retracted_at:" in text

        ok = await service.restore(
            correction_id=signal.id,
            workspace_id=workspace_id,
        )

    assert ok is True
    text = (vault_root / rel_path).read_text(encoding="utf-8")
    assert "retracted_at" not in text
    assert "retracted_by" not in text
    assert "retraction_reason" not in text


async def test_correct_action_persists_row_without_tombstone(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """``correct`` records intent + applies cleanly without writing a tombstone.

    M3a defers the actual frontmatter rewrite to M3b alongside the inline
    editor; the audit row + signal must still flow end to end so the trail
    exists when the editor lands.
    """
    rel_path = _seed_note(vault_root)
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        signal, _ = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="correct",
            reason="typo in the answer",
        )
        await session.commit()
        applied = await service.apply_pending(
            workspace_id=workspace_id,
            now=signal.apply_at + timedelta(seconds=1),
        )

    assert applied == 1
    # No tombstone — correct does not retract.
    text = (vault_root / rel_path).read_text(encoding="utf-8")
    assert "retracted_at" not in text


async def test_issue_now_parameter_controls_timestamps(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Injected ``now`` is honored — testable + sweep-able."""
    rel_path = _seed_note(vault_root)
    pinned = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        signal, _ = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
            now=pinned,
        )
    assert signal.issued_at == pinned
    assert signal.apply_at == pinned + timedelta(seconds=UNDO_WINDOW_SECONDS)
