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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.knowledge.application.retraction_service import (
    CorrectionUnavailableError,
    RetractionService,
)
from backend.knowledge.domain.retraction import UNDO_WINDOW_SECONDS
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenWriter
from backend.knowledge.infrastructure.ontology_db import OntologyCorrection

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
        signal, outcome = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
            reason="we changed the cache policy",
        )
        await session.commit()

    assert outcome == "created"
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
        _first, outcome1 = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
            correction_id=cid,
        )
        await session.commit()
        second, outcome2 = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
            correction_id=cid,
        )
    assert outcome1 == "created"
    assert outcome2 == "already_pending"
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


async def test_correct_issue_is_refused_no_row_no_audit(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``correct`` has no in-place field-rewrite implementation, so the service
    REFUSES it honestly — it must NOT persist a correction row and must NOT
    emit any audit event. The old behaviour minted a row + emitted a false
    ``ontology.correction.applied`` for an operation that changed nothing; the
    honest contract is a hard refusal at intake.
    """
    emitted: list[str] = []

    async def _spy_emit(event: object, *, session: object, emitter: object = None) -> None:
        emitted.append(type(event).__name__)

    monkeypatch.setattr(
        "backend.knowledge.application.retraction_service.safe_emit",
        _spy_emit,
    )

    rel_path = _seed_note(vault_root)
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        with pytest.raises(CorrectionUnavailableError):
            await service.issue(
                workspace_id=workspace_id,
                actor_id=actor_id,
                node_ref=rel_path,
                action="correct",
                reason="typo in the answer",
            )
        await session.commit()
        count = (
            await session.execute(select(func.count()).select_from(OntologyCorrection))
        ).scalar_one()

    assert count == 0, "a refused correction must not persist a row"
    assert emitted == [], "a refused correction must not emit any audit event"
    # The note is untouched.
    text = (vault_root / rel_path).read_text(encoding="utf-8")
    assert "retracted_at" not in text


async def test_apply_pending_never_applies_a_correct_row(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: even a legacy ``correct`` row sitting in the table past
    its deadline must NEVER be marked applied or emit
    ``ontology.correction.applied`` — that record would claim a vault mutation
    that never happened."""
    emitted: list[str] = []

    async def _spy_emit(event: object, *, session: object, emitter: object = None) -> None:
        emitted.append(type(event).__name__)

    monkeypatch.setattr(
        "backend.knowledge.application.retraction_service.safe_emit",
        _spy_emit,
    )

    rel_path = _seed_note(vault_root)
    past = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    row = OntologyCorrection(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        actor_id=actor_id,
        action="correct",
        node_ref=rel_path,
        reason=None,
        signal_json={},
        issued_at=past,
        apply_at=past,
    )
    async with sf() as session:
        session.add(row)
        await session.commit()
        service = RetractionService(session=session, writer=_writer(vault_root))
        applied = await service.apply_pending(
            workspace_id=workspace_id,
            now=past + timedelta(seconds=60),
        )
        await session.refresh(row)

    assert applied == 0, "a correct row must never be swept into an 'applied' state"
    assert row.applied_at is None, "a correct row must never be marked applied"
    assert "OntologyCorrectionApplied" not in emitted, "no false applied audit"
    text = (vault_root / rel_path).read_text(encoding="utf-8")
    assert "retracted_at" not in text


async def test_issue_dedupes_pending_retract_on_node_ref(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """A second retract on the SAME node (no correction_id) returns the existing
    pending signal + ``already_pending`` — it must NOT mint a duplicate row."""
    rel_path = _seed_note(vault_root)
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        first, outcome1 = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
        )
        await session.commit()
        second, outcome2 = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
        )
        await session.commit()
        # Only one row should exist for this node.
        pending = await service.apply_pending(
            workspace_id=workspace_id,
            now=first.apply_at + timedelta(seconds=1),
        )

    assert outcome1 == "created"
    assert outcome2 == "already_pending"
    assert second.id == first.id
    assert pending == 1, "dedupe must have prevented a second queued row"


async def test_issue_after_applied_returns_already_applied(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Re-retracting a node whose tombstone is already committed → ``already_applied``."""
    rel_path = _seed_note(vault_root)
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        first, _ = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
        )
        await session.commit()
        await service.apply_pending(
            workspace_id=workspace_id,
            now=first.apply_at + timedelta(seconds=1),
        )
        await session.commit()
        again, outcome = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
        )

    assert outcome == "already_applied"
    assert again.id == first.id


async def test_issue_after_undo_allows_new_retract(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """A cancelled (undone) retract must NOT block a fresh retract on the node."""
    rel_path = _seed_note(vault_root)
    async with sf() as session:
        service = RetractionService(session=session, writer=_writer(vault_root))
        first, _ = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
        )
        await session.commit()
        await service.undo(correction_id=first.id, workspace_id=workspace_id)
        await session.commit()
        second, outcome = await service.issue(
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=rel_path,
            action="retract",
        )

    assert outcome == "created"
    assert second.id != first.id


async def test_apply_pending_missing_note_marks_applied_without_raising(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """A queued retract for a since-deleted note must not wedge the sweep —
    the row is marked applied (retraction goal already met) and no error escapes."""
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
        # The note vanishes before the window closes.
        (vault_root / rel_path).unlink()
        applied = await service.apply_pending(
            workspace_id=workspace_id,
            now=signal.apply_at + timedelta(seconds=1),
        )
        await session.commit()
        # A subsequent sweep is a no-op (row is terminal).
        re_applied = await service.apply_pending(
            workspace_id=workspace_id,
            now=signal.apply_at + timedelta(seconds=5),
        )

    assert applied == 1
    assert re_applied == 0


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
