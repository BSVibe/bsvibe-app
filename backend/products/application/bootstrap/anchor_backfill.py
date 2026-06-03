"""Bootstrap canonical-anchor registration (Lift A-fix).

Diagnostic: Lift A's bootstrap wrote garden seedling notes + entity stubs to
the per-workspace vault, but did NOT register any of them as **canonical
anchors** (``concepts/active/<id>.md`` files). The PWA Knowledge graph view
sources its picture from
:func:`~backend.knowledge.canonicalization.concept_graph.build_concept_graph`
which reads :meth:`~backend.knowledge.canonicalization.index.InMemoryCanonicalizationIndex.list_active_concepts`
— a scan of ``concepts/active/``. Empty directory → empty graph, even when
the LLM-classified ingest produced thousands of high-signal entity files.

This module closes the gap by driving the **existing**
:class:`~backend.knowledge.canonicalization.promotion.GardenObservationPromoter`
over the workspace vault after bootstrap ingest:

1. The promoter collects content tags (drops structural ``settle`` /
   ``verified-run``) from every ``garden/**.md`` note.
2. Applies the recurrence gate (``>= 2`` distinct observations) — one-off
   noise stays uncreated and dies a natural death (founder policy:
   :ref:`bsvibe-noise-natural-decay`).
3. Routes each surviving candidate through
   :meth:`CanonicalizationService.resolve_and_canonicalize` with the default
   permissive (Safe Mode off) policy, which writes a
   ``concepts/active/<id>.md`` per tag.

Idempotent: the resolver dedups (an existing concept resolves, no new draft),
so re-running on a vault with anchors already present is a no-op.

Used by:

* :func:`backend.workflow.application.runtime.product_bootstrap_runtime.run_product_bootstrap_job`
  — invoked after ingest so newly written seedling tags promote in the same
  bootstrap pass.
* ``python -m backend.products backfill-anchors --product-slug X`` — one-shot
  retrofit for products bootstrapped before this fix landed.
"""

from __future__ import annotations

import structlog

from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.promotion import (
    GardenObservationPromoter,
    PromotionResult,
)
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import StorageBackend

logger = structlog.get_logger(__name__)


async def register_bootstrap_anchors(
    storage: StorageBackend,
) -> PromotionResult:
    """Promote a bootstrapped workspace's garden tags into canonical anchors.

    Takes a :class:`StorageBackend` rooted at the per-workspace vault
    (``<vault_root>/<region>/<workspace_id>/``) and runs one
    :class:`GardenObservationPromoter` pass with the default permissive
    policy (Safe Mode off). Returns the :class:`PromotionResult` so the caller
    can log how many concepts landed.

    Safe to call on a vault with no garden notes (returns an empty result).
    Safe to call repeatedly — the resolver dedups existing concepts.

    Failures inside the promotion pass propagate to the caller; the runtime
    layer wraps the whole call so a promotion failure becomes a soft warning
    (the seedlings + entity stubs are already on disk and the graph view will
    fill in later as more bootstrap passes run or the founder promotes
    manually).
    """
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    service = CanonicalizationService(
        store=NoteStore(storage),
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
        # safe_mode defaults to ``lambda: False`` → permissive auto-apply, so
        # ``apply_action`` lands the new concept synchronously rather than
        # queuing a ``pending_approval`` action that needs founder review.
        # For bootstrap this is correct: the founder asked us to bootstrap the
        # repo, so promoting LLM-extracted recurring entities under that same
        # consent is in scope. Founder can retract via the canonicalization
        # queue UI / merge UI later.
    )

    promoter = GardenObservationPromoter(service, actor="bootstrap-anchor-backfill")
    result = await promoter.promote()
    logger.info(
        "bootstrap_anchor_registration_complete",
        candidate_tags=len(result.candidate_tags),
        created_concepts=len(result.created_concepts),
        pending_actions=len(result.pending_actions),
        proposals=len(result.proposals),
        applied_merges=len(result.applied_merges),
    )
    return result


__all__ = ["PromotionResult", "register_bootstrap_anchors"]
