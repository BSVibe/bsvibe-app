"""Tests for PolicyResolver + default fixture bootstrap (Handoff §8.2-8.5)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.policies import PolicyResolver
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.markdown_utils import extract_frontmatter
from backend.knowledge.graph.storage import FileSystemStorage


@pytest.fixture
def storage(tmp_path: Path) -> FileSystemStorage:
    return FileSystemStorage(tmp_path)


@pytest.fixture
def store(storage: FileSystemStorage) -> NoteStore:
    return NoteStore(storage)


@pytest.fixture
async def index(storage: FileSystemStorage) -> InMemoryCanonicalizationIndex:
    idx = InMemoryCanonicalizationIndex()
    await idx.initialize(storage)
    return idx


@pytest.fixture
def resolver(
    index: InMemoryCanonicalizationIndex,
    store: NoteStore,
) -> PolicyResolver:
    return PolicyResolver(
        index=index,
        store=store,
        clock=lambda: datetime(2026, 5, 6, 16, 0, 0),
    )


class TestBootstrapDefaults:
    @pytest.mark.asyncio
    async def test_creates_three_default_policies(
        self,
        resolver: PolicyResolver,
        storage: FileSystemStorage,
        index: InMemoryCanonicalizationIndex,
    ) -> None:
        await resolver.bootstrap_defaults()

        # Per Handoff §8.3-8.5
        for kind in ("staleness", "merge-auto-apply", "decision-maturity"):
            path = f"decisions/policy/{kind}/conservative-default.md"
            assert await storage.exists(path), f"missing {path}"
            fm = extract_frontmatter(await storage.read(path))
            assert fm["status"] == "active"
            assert fm["profile_name"] == "conservative-default"
            # Policy params are kind-specific but always present
            assert "params" in fm

    @pytest.mark.asyncio
    async def test_idempotent(self, resolver: PolicyResolver, storage: FileSystemStorage) -> None:
        await resolver.bootstrap_defaults()
        # Snapshot first run
        first_runs: dict[str, str] = {}
        for kind in ("staleness", "merge-auto-apply", "decision-maturity"):
            path = f"decisions/policy/{kind}/conservative-default.md"
            first_runs[path] = await storage.read(path)
        # Re-run should not overwrite
        await resolver.bootstrap_defaults()
        for path, content in first_runs.items():
            assert await storage.read(path) == content

    @pytest.mark.asyncio
    async def test_merge_auto_apply_thresholds(
        self,
        resolver: PolicyResolver,
        storage: FileSystemStorage,
    ) -> None:
        await resolver.bootstrap_defaults()
        path = "decisions/policy/merge-auto-apply/conservative-default.md"
        fm = extract_frontmatter(await storage.read(path))
        # Per Handoff §8.5
        assert fm["params"]["hard_blocks"]["cannot_link_threshold"] == 0.85
        assert fm["params"]["safe_mode_on"]["auto_apply_threshold"] == 0.90

    @pytest.mark.asyncio
    async def test_decision_maturity_thresholds(
        self,
        resolver: PolicyResolver,
        storage: FileSystemStorage,
    ) -> None:
        await resolver.bootstrap_defaults()
        path = "decisions/policy/decision-maturity/conservative-default.md"
        fm = extract_frontmatter(await storage.read(path))
        # Per Handoff §8.4
        assert fm["params"]["thresholds"]["hard_block"] == 0.85
        assert fm["params"]["thresholds"]["review"] == 0.60


class TestSelectPolicy:
    @pytest.mark.asyncio
    async def test_select_returns_active_policy(
        self,
        resolver: PolicyResolver,
        index: InMemoryCanonicalizationIndex,
    ) -> None:
        await resolver.bootstrap_defaults()
        result = await resolver.select(kind="merge-auto-apply", scope={})
        assert result is not None
        assert result.profile_name == "conservative-default"

    @pytest.mark.asyncio
    async def test_select_no_policy_returns_none(self, resolver: PolicyResolver) -> None:
        assert await resolver.select(kind="merge-auto-apply", scope={}) is None

    @pytest.mark.asyncio
    async def test_select_highest_priority_wins(
        self,
        resolver: PolicyResolver,
        store: NoteStore,
        index: InMemoryCanonicalizationIndex,
    ) -> None:
        # Two active policies for same kind, different priority
        for prio, name in [(100, "low-prio"), (200, "high-prio")]:
            entry = models.PolicyEntry(
                path=f"decisions/policy/merge-auto-apply/{name}.md",
                kind="merge-auto-apply",
                status="active",
                profile_name=name,
                priority=prio,
                scope={},
                policy_schema_version="merge-auto-apply-policy-v1",
                valid_from=datetime(2026, 5, 6),
                params={},
                created_at=datetime(2026, 5, 6),
                updated_at=datetime(2026, 5, 6),
            )
            await store.write_policy(entry)
            await index.invalidate(entry.path)

        result = await resolver.select(kind="merge-auto-apply", scope={})
        assert result is not None
        assert result.profile_name == "high-prio"

    @pytest.mark.asyncio
    async def test_select_skips_expired(
        self,
        resolver: PolicyResolver,
        store: NoteStore,
        index: InMemoryCanonicalizationIndex,
    ) -> None:
        expired_entry = models.PolicyEntry(
            path="decisions/policy/staleness/expired-default.md",
            kind="staleness",
            status="active",
            profile_name="expired-default",
            priority=200,
            scope={},
            policy_schema_version="staleness-policy-v1",
            valid_from=datetime(2026, 5, 1),
            expires_at=datetime(2026, 5, 5),  # expired before clock
            params={},
            created_at=datetime(2026, 5, 1),
            updated_at=datetime(2026, 5, 1),
        )
        await store.write_policy(expired_entry)
        await index.invalidate(expired_entry.path)
        await resolver.bootstrap_defaults()

        result = await resolver.select(kind="staleness", scope={})
        assert result is not None
        assert result.profile_name == "conservative-default"

    @pytest.mark.asyncio
    async def test_select_skips_inactive(
        self,
        resolver: PolicyResolver,
        store: NoteStore,
        index: InMemoryCanonicalizationIndex,
    ) -> None:
        await resolver.bootstrap_defaults()
        # Add a higher-priority but draft policy — should not be selected
        draft = models.PolicyEntry(
            path="decisions/policy/merge-auto-apply/draft-policy.md",
            kind="merge-auto-apply",
            status="draft",
            profile_name="draft-policy",
            priority=999,
            scope={},
            policy_schema_version="merge-auto-apply-policy-v1",
            valid_from=datetime(2026, 5, 6),
            params={},
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
        )
        await store.write_policy(draft)
        await index.invalidate(draft.path)

        result = await resolver.select(kind="merge-auto-apply", scope={})
        assert result is not None
        assert result.profile_name == "conservative-default"
