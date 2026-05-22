"""Tests for service.expire_stale() (Handoff §13 step 3 + §15.3 canon-expire)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.knowledge._internal.events import EventBus, EventType
from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.decisions import DecisionMemory
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.policies import PolicyResolver
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.markdown_utils import extract_frontmatter
from backend.knowledge.graph.storage import FileSystemStorage


@pytest.fixture
def storage(tmp_path: Path) -> FileSystemStorage:
    return FileSystemStorage(tmp_path)


def _now() -> datetime:
    return datetime(2026, 5, 7, 14, 0, 0)


@pytest.fixture
async def service(storage: FileSystemStorage):
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    store = NoteStore(storage)
    return CanonicalizationService(
        store=store,
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
        decisions=DecisionMemory(index=index, store=store),
        policies=PolicyResolver(index=index, store=store, clock=_now),
        clock=_now,
    )


async def _seed_action(
    service: CanonicalizationService,
    *,
    kind: str,
    status: str,
    expires_at: datetime,
    slug: str,
) -> str:
    path = f"actions/{kind}/20260501-{slug}.md"
    await service._store.write_action(
        models.ActionEntry(
            path=path,
            kind=kind,
            status=status,
            action_schema_version=f"{kind}-v1",
            params={"concept": slug, "title": slug},
            created_at=datetime(2026, 5, 1),
            updated_at=datetime(2026, 5, 1),
            expires_at=expires_at,
        )
    )
    await service._index.invalidate(path)
    return path


async def _seed_proposal(
    service: CanonicalizationService,
    *,
    status: str,
    expires_at: datetime,
    slug: str,
) -> str:
    path = f"proposals/merge-concepts/20260501-{slug}.md"
    await service._store.write_proposal(
        models.ProposalEntry(
            path=path,
            kind="merge-concepts",
            status=status,
            strategy="deterministic",
            generator="deterministic-v1",
            generator_version="v1",
            proposal_score=0.5,
            created_at=datetime(2026, 5, 1),
            updated_at=datetime(2026, 5, 1),
            expires_at=expires_at,
        )
    )
    await service._index.invalidate(path)
    return path


class TestExpireActions:
    @pytest.mark.asyncio
    async def test_draft_action_past_expiry_flipped_to_expired(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        past = _now() - timedelta(days=1)
        path = await _seed_action(
            service, kind="create-concept", status="draft", expires_at=past, slug="ml"
        )
        result = await service.expire_stale()
        assert path in result.expired_actions
        fm = extract_frontmatter(await storage.read(path))
        assert fm["status"] == "expired"

    @pytest.mark.asyncio
    async def test_pending_approval_past_expiry_flipped(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        past = _now() - timedelta(days=1)
        path = await _seed_action(
            service,
            kind="create-concept",
            status="pending_approval",
            expires_at=past,
            slug="x",
        )
        result = await service.expire_stale()
        assert path in result.expired_actions

    @pytest.mark.asyncio
    async def test_action_within_expiry_window_left_alone(
        self, service: CanonicalizationService
    ) -> None:
        future = _now() + timedelta(days=1)
        path = await _seed_action(
            service, kind="create-concept", status="draft", expires_at=future, slug="x"
        )
        result = await service.expire_stale()
        assert path not in result.expired_actions

    @pytest.mark.asyncio
    async def test_terminal_status_skipped(self, service: CanonicalizationService) -> None:
        # applied / rejected / expired / superseded / failed must not be re-flipped
        past = _now() - timedelta(days=10)
        for status in ("applied", "rejected", "expired", "superseded", "failed"):
            await _seed_action(
                service,
                kind="create-concept",
                status=status,
                expires_at=past,
                slug=f"a-{status}",
            )
        result = await service.expire_stale()
        assert result.expired_actions == []


class TestExpireProposals:
    @pytest.mark.asyncio
    async def test_pending_proposal_past_expiry_flipped(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        past = _now() - timedelta(days=1)
        path = await _seed_proposal(service, status="pending", expires_at=past, slug="x")
        result = await service.expire_stale()
        assert path in result.expired_proposals
        fm = extract_frontmatter(await storage.read(path))
        assert fm["status"] == "expired"

    @pytest.mark.asyncio
    async def test_terminal_proposal_skipped(self, service: CanonicalizationService) -> None:
        past = _now() - timedelta(days=10)
        for status in ("accepted", "rejected", "superseded", "expired"):
            await _seed_proposal(service, status=status, expires_at=past, slug=f"p-{status}")
        result = await service.expire_stale()
        assert result.expired_proposals == []


class TestExpireEmitsEvents:
    @pytest.mark.asyncio
    async def test_expiry_emits_status_change_events(self, storage: FileSystemStorage) -> None:
        captured = []

        class _Cap:
            async def on_event(self, event):
                captured.append(event)

        bus = EventBus()
        bus.subscribe(_Cap())

        index = InMemoryCanonicalizationIndex()
        await index.initialize(storage)
        store = NoteStore(storage)
        svc = CanonicalizationService(
            store=store,
            lock=AsyncIOMutationLock(),
            index=index,
            resolver=TagResolver(index=index),
            decisions=DecisionMemory(index=index, store=store),
            policies=PolicyResolver(index=index, store=store, clock=_now),
            clock=_now,
            event_bus=bus,
        )

        past = _now() - timedelta(days=1)
        ap = await _seed_action(
            svc, kind="create-concept", status="draft", expires_at=past, slug="ml"
        )
        pp = await _seed_proposal(svc, status="pending", expires_at=past, slug="x")
        captured.clear()

        await svc.expire_stale()

        action_changes = [
            e
            for e in captured
            if e.event_type == EventType.CANONICALIZATION_ACTION_STATUS_CHANGED
            and e.payload.get("status") == "expired"
        ]
        proposal_changes = [
            e
            for e in captured
            if e.event_type == EventType.CANONICALIZATION_PROPOSAL_STATUS_CHANGED
            and e.payload.get("status") == "expired"
        ]
        assert any(e.payload.get("path") == ap for e in action_changes)
        assert any(e.payload.get("path") == pp for e in proposal_changes)


class TestNowOverride:
    @pytest.mark.asyncio
    async def test_explicit_now_lets_caller_force_expiry(
        self, service: CanonicalizationService
    ) -> None:
        # Default clock is 2026-05-07; expires_at is 5 days in the future
        # relative to default clock. Calling with a now far enough ahead
        # should flip it.
        future_expiry = _now() + timedelta(days=2)
        path = await _seed_action(
            service,
            kind="create-concept",
            status="draft",
            expires_at=future_expiry,
            slug="ml",
        )
        # With default now, no expiry
        assert (await service.expire_stale()).expired_actions == []
        # With explicit later now, expired
        late = future_expiry + timedelta(days=1)
        result = await service.expire_stale(now=late)
        assert path in result.expired_actions
