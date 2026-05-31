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

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.models import DecisionEntry
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.infrastructure.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
    build_garden_promoter_factory,
)
from backend.workers.db import SettleDrainRow, WorkersBase
from backend.workflow.infrastructure.db import (
    ExecutionBase,
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
)
from backend.workspaces.db import WorkspaceRow, WorkspacesBase

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_BASES = (ExecutionBase, WorkersBase, WorkspacesBase)
_REGION = "us-1"


@pytest_asyncio.fixture
async def sf():
    async with db_engine(*_BASES) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


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
    product_slug: str | None = None,
    product_name: str | None = None,
    intent_text: str | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    activity_id = uuid.uuid4()
    payload: dict = {
        "verified": True,
        "artifact_refs": refs if refs is not None else ["deploy/Caddyfile"],
        "summary": summary,
    }
    # Mirror the orchestrator's enriched emission: only present keys are written
    # (a connector-inbound run carries neither).
    if product_slug is not None:
        payload["product_slug"] = product_slug
    if product_name is not None:
        payload["product_name"] = product_name
    if intent_text is not None:
        payload["intent_text"] = intent_text
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
                payload=payload,
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
    proposer has a real cluster + a non-cluster to promote. Every entity recurs
    across **>= 2 distinct observations** so it clears the promoter's recurrence
    gate (``_MIN_OBSERVATIONS_FOR_PROMOTION``); a candidate must be a genuinely
    recurring pattern, which is what these fixtures represent.
    """
    for i in range(4):
        await storage.write(
            f"garden/seedling/obs-self-hosting-{i}.md",
            "---\ntags:\n  - settle\n  - verified-run\n  - self-hosting\n---\n# obs\n",
        )
    for i in range(2):
        await storage.write(
            f"garden/seedling/obs-self-host-{i}.md",
            "---\ntags:\n  - settle\n  - verified-run\n  - self-host\n---\n# obs\n",
        )
    for i in range(2):
        await storage.write(
            f"garden/seedling/obs-vaultwarden-{i}.md",
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


async def test_drain_then_promote_safe_mode_auto_applies_low_risk(sf, tmp_path) -> None:
    """Safe-Mode workspace, risk-aware gate: drain writes the observation, and
    the promotion pass AUTO-APPLIES low-risk create-concepts (no conflict) even
    under Safe Mode. Adding fresh knowledge is not 'risky' — Safe Mode is for
    genuine conflicts, not routine knowledge accrual.

    Before the risk-aware gate, Safe Mode blanket-queued every action and
    ``concepts/active`` stayed empty; that was the wrong criterion (it equated
    'add knowledge' with 'risk'). The wired scorer + policy now let clean
    create-concepts settle automatically.
    """
    ws = uuid.uuid4()
    await _add_workspace(sf, workspace_id=ws, safe_mode=True)
    await _seed_content_tagged_observations(_ws_storage(tmp_path, ws))
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
    # Risk-aware Safe Mode: low-risk concepts auto-applied → active concepts exist.
    active = {
        p.removeprefix("concepts/active/").removesuffix(".md")
        for p in await storage.list_files("concepts/active")
    }
    # The unrelated clean concept settles (the self-host* pair may fold via a
    # clean merge, leaving one survivor — either way the graph is no longer empty).
    assert "vaultwarden" in active, active
    # The sink's single observation contributes its content tags ONCE, so the
    # recurrence gate (>= 2 observations) correctly withholds them from promotion
    # — a one-off run is not yet a settled pattern. They become anchors only once
    # the same tags recur across another run.
    assert {"configured", "reverse", "proxy", "caddyfile"}.isdisjoint(active), active
    # Structural markers never become concepts.
    assert "settle" not in active
    assert "verified-run" not in active


async def test_drain_then_promote_safe_mode_queues_conflicting_merge(sf, tmp_path) -> None:
    """Safe Mode still gates GENUINE risk: a merge that contradicts an active
    cannot-link decision (review band) is queued for approval, not auto-applied,
    even though clean create-concepts auto-apply in the same pass."""
    ws = uuid.uuid4()
    await _add_workspace(sf, workspace_id=ws, safe_mode=True)
    storage = _ws_storage(tmp_path, ws)
    await _seed_content_tagged_observations(storage)

    # Pre-seed a cannot-link decision between the two variant spellings so the
    # promoter's merge proposal scores into the review band → must queue.
    decision = DecisionEntry(
        path="decisions/cannot-link/20260524-120000-self-hosting-self-host.md",
        kind="cannot-link",
        status="active",
        maturity="seedling",
        decision_schema_version="cannot-link-v1",
        subjects=("self-hosting", "self-host"),
        base_confidence=0.70,
        last_confirmed_at=datetime(2026, 5, 24, tzinfo=UTC),
        decay_profile="definitional",
        decay_halflife_days=None,
        valid_from=datetime(2026, 5, 24, tzinfo=UTC),
        created_at=datetime(2026, 5, 24, tzinfo=UTC),
        updated_at=datetime(2026, 5, 24, tzinfo=UTC),
    )
    await NoteStore(storage).write_decision(decision)

    await _seed_settle_activity(sf, workspace_id=ws, summary="hardened the proxy")

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region=_REGION),
        promoter_factory=build_garden_promoter_factory(vault_root=tmp_path),
    )
    assert await worker.drain_once() == 1

    # Clean concepts auto-applied (the variant pair both exist as anchors) ...
    active = {
        p.removeprefix("concepts/active/").removesuffix(".md")
        for p in await storage.list_files("concepts/active")
    }
    assert {"self-hosting", "self-host"} <= active, active
    # ... but the conflicting merge did NOT apply — neither was folded away.
    assert await storage.list_files("concepts/merged") == []
    merge_actions = await storage.list_files("actions/merge-concepts")
    assert merge_actions, "a merge action should have been drafted"
    from backend.knowledge.graph.markdown_utils import extract_frontmatter

    statuses = {extract_frontmatter(await storage.read(p))["status"] for p in merge_actions}
    assert statuses == {"pending_approval"}, statuses


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


async def test_promotion_idempotent_across_repeated_drains(sf, tmp_path) -> None:
    """Promotion is idempotent: once a pattern has recurred enough to settle,
    further drains of identical content add no duplicate concepts.

    With the recurrence gate, the sink's content tags must appear in >= 2
    observations before they promote — so we drain TWICE with identical content
    to reach steady state, snapshot, then drain a THIRD time and assert the
    active concept set is unchanged (a *new* summary would correctly add new
    concepts — that's new knowledge, not a duplicate)."""
    ws = uuid.uuid4()
    await _add_workspace(sf, workspace_id=ws, safe_mode=False)
    await _seed_content_tagged_observations(_ws_storage(tmp_path, ws))

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region=_REGION),
        promoter_factory=build_garden_promoter_factory(vault_root=tmp_path),
    )
    storage = _ws_storage(tmp_path, ws)

    # Two drains of identical content → the sink's derived tags now recur across
    # two observations and clear the gate, reaching steady state.
    for _ in range(2):
        await _seed_settle_activity_with_refs(
            sf, workspace_id=ws, summary="hardened the proxy", refs=["deploy/Caddyfile"]
        )
        assert await worker.drain_once() == 1
    active_steady = sorted(await storage.list_files("concepts/active"))
    assert active_steady  # promotion produced anchors

    # A third settle activity with identical content → another promotion pass
    # that must add no new concepts.
    await _seed_settle_activity_with_refs(
        sf, workspace_id=ws, summary="hardened the proxy", refs=["deploy/Caddyfile"]
    )
    assert await worker.drain_once() == 1

    active_after_third = sorted(await storage.list_files("concepts/active"))
    assert active_after_third == active_steady, "promotion must be idempotent"


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


async def test_loop_clusters_two_runs_by_shared_product_and_intent(sf, tmp_path) -> None:
    """The product+intent enrichment closes the gap PR #27 flagged.

    Two settle activities for the SAME product (slug ``vaultwarden-selfhost``)
    and SAME founder intent, but touching DIFFERENT files (``deploy/Caddyfile``
    vs ``backend/auth/client.py``) with non-overlapping summaries. The PR #27
    derivation (file stems + summary words) would give the two runs ZERO shared
    content tag — no cluster. With product+intent threaded in, both runs carry
    the product slug + intent terms as the leading content tags, so the promoter
    folds them onto canonical anchors keyed on what the work was ABOUT (the
    product / intent), not which files happened to change.
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

    # Run 1: a settle for the product, touching the deploy config.
    await _seed_settle_activity(
        sf,
        workspace_id=ws,
        summary="hardened the deploy config",
        refs=["deploy/Caddyfile"],
        product_slug="vaultwarden-selfhost",
        intent_text="Set up the vaultwarden password manager",
    )
    assert await worker.drain_once() == 1

    # Run 2: SAME product + intent, but completely different files + summary —
    # zero overlap on the PR #27 (file/summary) signal alone.
    await _seed_settle_activity(
        sf,
        workspace_id=ws,
        summary="refactored the token rotation logic",
        refs=["backend/auth/client.py"],
        product_slug="vaultwarden-selfhost",
        intent_text="Set up the vaultwarden password manager",
    )
    assert await worker.drain_once() == 1

    # Both settle notes were written (loop half 1) ...
    assert len(_written_settle_notes(tmp_path, ws)) == 2

    # ... and the promoter produced a canonical anchor keyed on the SHARED
    # product slug + a shared intent term — the cross-run cluster the PR #27
    # signal alone could not form.
    active = {
        p.removeprefix("concepts/active/").removesuffix(".md")
        for p in await storage.list_files("concepts/active")
    }
    assert "vaultwarden-selfhost" in active, active
    assert "vaultwarden" in active, active
    assert "settle" not in active
    assert "verified-run" not in active

    # Deterministic retrieval resolves the product cluster key to its anchor.
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    resolver = TagResolver(index=index)
    resolved = await resolver.resolve("vaultwarden-selfhost")
    assert resolved.status == "resolved"
    assert resolved.concept_id == "vaultwarden-selfhost"


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
