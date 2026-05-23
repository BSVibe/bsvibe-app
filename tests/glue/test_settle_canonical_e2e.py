"""End-to-end glue: SettleWorker drain → garden notes → canonical promotion.

PR #9 wired ``SettleWorker`` to deposit each verified work step as a BSage
garden observation; PR #20 built ``GardenObservationPromoter`` to promote
recurring patterns into canonical anchors. Until now the two halves of the §5
trust ratchet were both built but NOT connected at runtime — the worker never
invoked the promoter. This suite proves the loop now closes inside the running
worker:

    settle activities
        -> SettleWorker.drain_once
            -> KnowledgeSettleSink writes garden observations (real vault write)
            -> per affected workspace: GardenObservationPromoter.promote()
                -> permissive policy  -> canonical anchors + merged concept
                -> Safe-Mode default  -> create-concept proposals queued
        (a promoter failure is soft — drain count + notes survive)

Promotion is exercised over the SAME per-workspace vault boundary the sink
writes to (``<vault_root>/<region>/<workspace_id>/`` via the KnowledgeFactory
convention) using the production ``build_garden_promoter_factory``.

Note on the seeded content-tagged observations: ``KnowledgeSettleSink`` stamps
only the *structural* tags (``settle`` / ``verified-run``) on its notes, which
the promoter intentionally drops — so a sink note alone yields no candidate to
promote. To prove promotion does real work over the wired boundary, we seed
content-tagged garden observations into the workspace vault (exactly as a
richer producer would), then drive the drain so the worker's promotion pass
runs over that same vault. The sink's own note is still written (loop half 1)
and correctly contributes no candidate tag.

No real LLM / network: the sink is a plain markdown write and the proposer is
purely lexical (character-trigram Jaccard).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.execution.db import ExecutionBase, ExecutionRun, ExecutionRunActivity, RunStatus
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.graph.storage import FileSystemStorage
from backend.workers.db import SettleDrainRow, WorkersBase
from backend.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
    build_garden_promoter_factory,
)
from backend.workspaces.db import WorkspaceRow, WorkspacesBase

PG_URL = os.environ.get(
    "BSVIBE_DATABASE_URL", "postgresql+asyncpg://bsvibe:bsvibe@localhost:5442/bsvibe"
)

pytestmark = pytest.mark.asyncio

_BASES = (ExecutionBase, WorkersBase, WorkspacesBase)
_REGION = "us-1"


def _can_reach_pg() -> bool:
    sync_url = PG_URL.replace("+asyncpg", "+psycopg") if "+asyncpg" in PG_URL else PG_URL
    try:
        engine = create_engine(sync_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def sf():
    use_pg = bool(os.environ.get("BSVIBE_DATABASE_URL")) and _can_reach_pg()
    url = PG_URL if use_pg else "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        for base in _BASES:
            await conn.run_sync(base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    if use_pg:
        async with engine.begin() as conn:
            for base in reversed(_BASES):
                await conn.run_sync(base.metadata.drop_all)
    await engine.dispose()


async def _add_workspace(
    sf, *, workspace_id: uuid.UUID, region: str = _REGION, safe_mode: bool
) -> None:
    async with sf() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region=region, safe_mode=safe_mode))
        await s.commit()


async def _seed_settle_activity(
    sf,
    *,
    workspace_id: uuid.UUID,
    summary: str = "configured reverse proxy",
    refs: list[str] | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    activity_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.REVIEW_READY,
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            ExecutionRunActivity(
                id=activity_id,
                run_id=run_id,
                workspace_id=workspace_id,
                activity_type="settle",
                payload={
                    "verified": True,
                    "artifact_refs": refs if refs is not None else ["deploy/Caddyfile"],
                    "summary": summary,
                },
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return activity_id


async def _seed_settle_activity_with_refs(
    sf, *, workspace_id: uuid.UUID, summary: str, refs: list[str]
) -> uuid.UUID:
    """Thin alias making the recurring-artifact intent explicit at call sites."""
    return await _seed_settle_activity(sf, workspace_id=workspace_id, summary=summary, refs=refs)


def _ws_storage(vault_root: Path, workspace_id: uuid.UUID) -> FileSystemStorage:
    """Storage rooted exactly like KnowledgeFactory + the promoter factory."""
    ws_root = vault_root / _REGION / str(workspace_id)
    ws_root.mkdir(parents=True, exist_ok=True)
    return FileSystemStorage(ws_root)


async def _seed_content_tagged_observations(storage: FileSystemStorage) -> None:
    """Seed settle-style garden observations carrying content tags.

    One entity under two variant spellings (``self-hosting`` / ``self-host``)
    plus an unrelated entity — mirrors the PR #20 promotion e2e fixture so the
    proposer has a real cluster + a non-cluster to promote.
    """
    for i in range(4):
        await storage.write(
            f"garden/seedling/obs-self-hosting-{i}.md",
            "---\ntags:\n  - settle\n  - verified-run\n  - self-hosting\n---\n# obs\n",
        )
    await storage.write(
        "garden/seedling/obs-self-host.md",
        "---\ntags:\n  - settle\n  - verified-run\n  - self-host\n---\n# obs\n",
    )
    await storage.write(
        "garden/seedling/obs-vaultwarden.md",
        "---\ntags:\n  - settle\n  - verified-run\n  - vaultwarden\n---\n# obs\n",
    )


def _written_settle_notes(vault_root: Path, workspace_id: uuid.UUID) -> list[Path]:
    """Notes the sink wrote this drain (titled ``Settle: ...``)."""
    ws_dir = vault_root / _REGION / str(workspace_id)
    if not ws_dir.exists():
        return []
    return [p for p in ws_dir.rglob("*.md") if p.name.startswith("settle-")]


async def test_drain_then_promote_permissive_creates_canonical_anchor(sf, tmp_path) -> None:
    """Permissive workspace: drain writes the observation AND the promotion pass
    folds the variant spellings onto one canonical concept."""
    ws = uuid.uuid4()
    await _add_workspace(sf, workspace_id=ws, safe_mode=False)
    await _seed_content_tagged_observations(_ws_storage(tmp_path, ws))
    await _seed_settle_activity(sf, workspace_id=ws, summary="hardened the proxy")

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region=_REGION),
        promoter_factory=build_garden_promoter_factory(vault_root=tmp_path),
    )
    processed = await worker.drain_once()

    # Loop half 1: the sink deposited the settle observation.
    assert processed == 1
    assert len(_written_settle_notes(tmp_path, ws)) == 1

    # Loop half 2: the promoter ran over the SAME workspace vault and applied a
    # merge — exactly one of the variant pair survives as the canonical anchor,
    # the unrelated entity is untouched.
    storage = _ws_storage(tmp_path, ws)
    active = {
        p.removeprefix("concepts/active/").removesuffix(".md")
        for p in await storage.list_files("concepts/active")
    }
    assert "vaultwarden" in active
    self_host_survivors = active & {"self-hosting", "self-host"}
    assert len(self_host_survivors) == 1, active
    merged = ({"self-hosting", "self-host"} - self_host_survivors).pop()
    assert await storage.exists(f"concepts/merged/{merged}.md")

    # Deterministic retrieval: both variant spellings resolve to the survivor.
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    resolver = TagResolver(index=index)
    canonical = next(iter(self_host_survivors))
    for variant in ("self-hosting", "self-host"):
        resolved = await resolver.resolve(variant)
        assert resolved.status == "resolved", (variant, resolved)
        assert resolved.concept_id == canonical, (variant, resolved.concept_id)


async def test_drain_then_promote_safe_mode_queues_proposals(sf, tmp_path) -> None:
    """Safe-Mode workspace (the default policy): drain writes the observation,
    promotion QUEUES create-concept actions and applies NO anchors."""
    ws = uuid.uuid4()
    await _add_workspace(sf, workspace_id=ws, safe_mode=True)
    await _seed_content_tagged_observations(_ws_storage(tmp_path, ws))
    # The sink note's own content tags now also become candidates — the gap this
    # PR closes. Use a known summary/ref so the candidate set is deterministic.
    await _seed_settle_activity_with_refs(
        sf, workspace_id=ws, summary="configured the reverse proxy", refs=["deploy/Caddyfile"]
    )

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region=_REGION),
        promoter_factory=build_garden_promoter_factory(vault_root=tmp_path),
    )
    processed = await worker.drain_once()

    assert processed == 1
    assert len(_written_settle_notes(tmp_path, ws)) == 1

    storage = _ws_storage(tmp_path, ws)
    # Safe Mode: nothing applied — no active concepts exist.
    assert await storage.list_files("concepts/active") == []
    # ... but create-concept actions are QUEUED for review. The candidate set is
    # the seeded content tags PLUS the sink note's own derived tags (the gap this
    # PR closes — the sink note is no longer a no-op for promotion).
    # Action filenames are ``YYYYMMDD-HHMMSS-<slug>.md`` — drop the two
    # timestamp segments to recover the concept slug (which may itself contain
    # hyphens, e.g. ``self-hosting``).
    create_slugs = {
        p.removeprefix("actions/create-concept/").removesuffix(".md").split("-", 2)[-1]
        for p in await storage.list_files("actions/create-concept")
    }
    # Seeded content tags are queued ...
    assert {"vaultwarden"} <= create_slugs, create_slugs
    # ... and the sink note's own derived content tags are queued too.
    assert {"configured", "reverse", "proxy", "caddyfile"} <= create_slugs, create_slugs
    # Structural markers never become candidates.
    assert "settle" not in create_slugs
    assert "verified-run" not in create_slugs


async def test_promotion_failure_is_soft_and_does_not_break_drain(sf, tmp_path) -> None:
    """A promoter that raises must NOT revert the settle write or change the
    drain count — settlement notes are the source of truth, promotion derived."""
    ws = uuid.uuid4()
    await _add_workspace(sf, workspace_id=ws, safe_mode=False)
    await _seed_settle_activity(sf, workspace_id=ws, summary="wired the cache")

    class _BoomPromoter:
        async def promote(self) -> object:
            raise RuntimeError("canon engine exploded")

    def _boom_factory(*, region: str, workspace_id: uuid.UUID, safe_mode: bool):
        return _BoomPromoter()

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region=_REGION),
        promoter_factory=_boom_factory,
    )
    processed = await worker.drain_once()

    # Drain is intact despite the promotion failure.
    assert processed == 1
    notes = _written_settle_notes(tmp_path, ws)
    assert len(notes) == 1
    assert "wired the cache" in notes[0].read_text(encoding="utf-8")
    # The activity is still marked drained (write succeeded, promotion is derived).
    async with sf() as s:
        drains = (await s.execute(select(SettleDrainRow))).scalars().all()
        assert len(drains) == 1


async def test_promotion_runs_per_affected_workspace_in_isolation(sf, tmp_path) -> None:
    """Two workspaces in one batch each get their own promotion pass over their
    own vault; one workspace's promotion failure can't stop the other's."""
    ws_ok = uuid.uuid4()
    ws_boom = uuid.uuid4()
    await _add_workspace(sf, workspace_id=ws_ok, safe_mode=False)
    await _add_workspace(sf, workspace_id=ws_boom, safe_mode=False)
    await _seed_content_tagged_observations(_ws_storage(tmp_path, ws_ok))
    await _seed_settle_activity(sf, workspace_id=ws_ok)
    await _seed_settle_activity(sf, workspace_id=ws_boom)

    real_factory = build_garden_promoter_factory(vault_root=tmp_path)

    class _BoomPromoter:
        async def promote(self) -> object:
            raise RuntimeError("boom")

    def _selective_factory(*, region: str, workspace_id: uuid.UUID, safe_mode: bool):
        if workspace_id == ws_boom:
            return _BoomPromoter()
        return real_factory(region=region, workspace_id=workspace_id, safe_mode=safe_mode)

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region=_REGION),
        promoter_factory=_selective_factory,
    )
    processed = await worker.drain_once()

    # Both settle activities drained regardless of the failing promotion.
    assert processed == 2
    # ws_ok's promotion still produced canonical concepts despite ws_boom failing.
    ok_storage = _ws_storage(tmp_path, ws_ok)
    assert await ok_storage.list_files("concepts/active") != []


async def test_promotion_idempotent_across_two_drains(sf, tmp_path) -> None:
    """Promotion is idempotent: a second drain batch in the same workspace runs
    the promoter again and adds no duplicate concepts."""
    ws = uuid.uuid4()
    await _add_workspace(sf, workspace_id=ws, safe_mode=False)
    await _seed_content_tagged_observations(_ws_storage(tmp_path, ws))
    # Identical summary + ref across both drains: the sink derives the SAME
    # content tags each time, so a re-run is genuinely a no-op for promotion
    # (a *new* summary would correctly add new concepts — that's new knowledge,
    # not a duplicate).
    await _seed_settle_activity_with_refs(
        sf, workspace_id=ws, summary="hardened the proxy", refs=["deploy/Caddyfile"]
    )

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region=_REGION),
        promoter_factory=build_garden_promoter_factory(vault_root=tmp_path),
    )
    assert await worker.drain_once() == 1
    storage = _ws_storage(tmp_path, ws)
    active_after_first = sorted(await storage.list_files("concepts/active"))
    assert active_after_first  # promotion produced anchors

    # A second settle activity with identical content → another promotion pass
    # that must add no new concepts.
    await _seed_settle_activity_with_refs(
        sf, workspace_id=ws, summary="hardened the proxy", refs=["deploy/Caddyfile"]
    )
    assert await worker.drain_once() == 1

    active_after_second = sorted(await storage.list_files("concepts/active"))
    assert active_after_second == active_after_first, "promotion must be idempotent"


async def test_loop_produces_canon_from_sink_derived_tags_no_seeding(sf, tmp_path) -> None:
    """The closed loop end-to-end with NO seeded content notes.

    Two settle activities across runs reference the SAME artifact
    (``backend/auth/client.py``) + overlapping summary terms. The sink derives
    content tags (``auth`` / ``client`` / ...) onto its own garden notes, so the
    promoter — running over the sink's own writes only — gets real candidates
    and applies canonical anchors. This proves the gap PR #23 left (sink wrote
    only structural tags → zero candidates) is actually closed in the running
    worker, not just when a richer producer seeds content-tagged notes.
    """
    ws = uuid.uuid4()
    await _add_workspace(sf, workspace_id=ws, safe_mode=False)  # permissive → apply

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region=_REGION),
        promoter_factory=build_garden_promoter_factory(vault_root=tmp_path),
    )
    storage = _ws_storage(tmp_path, ws)

    # Run 1: first settle referencing the recurring artifact.
    await _seed_settle_activity_with_refs(
        sf, workspace_id=ws, summary="configured auth client", refs=["backend/auth/client.py"]
    )
    assert await worker.drain_once() == 1

    # Run 2: a second settle in the same workspace, same artifact recurring.
    await _seed_settle_activity_with_refs(
        sf,
        workspace_id=ws,
        summary="hardened auth client refresh",
        refs=["backend/auth/client.py"],
    )
    assert await worker.drain_once() == 1

    # Both settle notes were written (loop half 1) ...
    assert len(_written_settle_notes(tmp_path, ws)) == 2
    # ... and the promoter produced canon (loop half 2) from the SINK's own
    # derived content tags — the recurring artifact stems are now canonical
    # anchors. No structural marker ever became a concept.
    active = {
        p.removeprefix("concepts/active/").removesuffix(".md")
        for p in await storage.list_files("concepts/active")
    }
    assert {"auth", "client"} <= active, active
    assert "settle" not in active
    assert "verified-run" not in active

    # Deterministic retrieval resolves the recurring pattern to its anchor.
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    resolver = TagResolver(index=index)
    resolved = await resolver.resolve("auth")
    assert resolved.status == "resolved"
    assert resolved.concept_id == "auth"


async def test_no_promoter_factory_disables_promotion(sf, tmp_path) -> None:
    """With no promoter_factory wired, the drain still works but no canon is
    produced — promotion is strictly opt-in via the factory."""
    ws = uuid.uuid4()
    await _add_workspace(sf, workspace_id=ws, safe_mode=False)
    await _seed_content_tagged_observations(_ws_storage(tmp_path, ws))
    await _seed_settle_activity(sf, workspace_id=ws)

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region=_REGION),
    )
    assert await worker.drain_once() == 1
    storage = _ws_storage(tmp_path, ws)
    assert await storage.list_files("concepts/active") == []
    assert await storage.list_files("actions/create-concept") == []
