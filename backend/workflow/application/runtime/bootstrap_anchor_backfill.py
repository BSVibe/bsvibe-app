"""One-shot CLI: retrofit canonical anchors for already-bootstrapped products.

Lift A-fix companion — before this fix landed,
:func:`run_product_bootstrap_job` finished ingest WITHOUT promoting any
recurring tags into ``concepts/active/<id>.md`` canonical anchors, so the PWA
Knowledge graph view stayed empty for every product bootstrapped on the old
code path. This CLI retrofits anchors for those products in one pass.

Usage::

    python -m backend.workflow.application.runtime.bootstrap_anchor_backfill \
        --product-slug bsvibe-app

    # or by workspace id (every product in the workspace):
    python -m backend.workflow.application.runtime.bootstrap_anchor_backfill \
        --workspace-id 11111111-1111-1111-1111-111111111111

    # dry-run lists the workspaces it would touch without mutating the vault:
    python -m backend.workflow.application.runtime.bootstrap_anchor_backfill \
        --product-slug bsvibe-app --dry-run

Idempotent: re-running on a vault that already has anchors is a no-op (the
canonicalization resolver dedups existing concepts).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import get_settings
from backend.data.session import make_engine
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.knowledge.graph.storage import FileSystemStorage
from backend.products.application.bootstrap import register_bootstrap_anchors
from backend.shared.core.logging import configure_logging

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Target:
    """One workspace vault to retrofit."""

    workspace_id: uuid.UUID
    region: str
    product_slug: str
    product_id: uuid.UUID


async def _resolve_targets(
    session_factory: async_sessionmaker,
    *,
    product_slug: str | None,
    workspace_id: uuid.UUID | None,
) -> list[_Target]:
    """Resolve CLI args into the set of workspace vaults to retrofit."""
    async with session_factory() as session:
        stmt = (
            select(ProductRow, WorkspaceRow)
            .join(WorkspaceRow, ProductRow.workspace_id == WorkspaceRow.id)
            .where(ProductRow.bootstrap_status == "complete")
        )
        if product_slug is not None:
            stmt = stmt.where(ProductRow.slug == product_slug)
        if workspace_id is not None:
            stmt = stmt.where(ProductRow.workspace_id == workspace_id)
        rows = (await session.execute(stmt)).all()

    return [
        _Target(
            workspace_id=ws.id,
            region=ws.region,
            product_slug=p.slug,
            product_id=p.id,
        )
        for (p, ws) in rows
    ]


async def _retrofit_target(target: _Target, vault_root: Path, *, dry_run: bool) -> None:
    """Run anchor registration against one workspace vault."""
    workspace_vault = vault_root / target.region / str(target.workspace_id)
    if not workspace_vault.exists():
        logger.warning(
            "anchor_backfill_vault_missing",
            workspace_id=str(target.workspace_id),
            region=target.region,
            product_slug=target.product_slug,
        )
        return

    if dry_run:
        logger.info(
            "anchor_backfill_dry_run",
            workspace_id=str(target.workspace_id),
            region=target.region,
            product_slug=target.product_slug,
            vault=str(workspace_vault),
        )
        return

    storage = FileSystemStorage(workspace_vault)
    result = await register_bootstrap_anchors(storage)
    logger.info(
        "anchor_backfill_done",
        workspace_id=str(target.workspace_id),
        region=target.region,
        product_slug=target.product_slug,
        candidate_tags=len(result.candidate_tags),
        created_concepts=len(result.created_concepts),
    )


async def run_backfill(
    *,
    session_factory: async_sessionmaker,
    product_slug: str | None,
    workspace_id: uuid.UUID | None,
    vault_root: Path,
    dry_run: bool,
) -> int:
    """Top-level coroutine — resolve targets, then retrofit each.

    Returns the number of workspace vaults processed (or that would be
    processed in dry-run mode). A return of ``0`` means the lookup matched
    nothing and is surfaced to the caller as a non-zero exit code so the
    operator notices a typo.
    """
    targets = await _resolve_targets(
        session_factory,
        product_slug=product_slug,
        workspace_id=workspace_id,
    )
    if not targets:
        logger.warning(
            "anchor_backfill_no_targets",
            product_slug=product_slug,
            workspace_id=str(workspace_id) if workspace_id else None,
        )
        return 0
    for target in targets:
        await _retrofit_target(target, vault_root, dry_run=dry_run)
    return len(targets)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bootstrap_anchor_backfill",
        description=(
            "Retrofit canonical anchors for already-bootstrapped products (Lift A-fix one-shot)."
        ),
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--product-slug",
        help="Retrofit only the product with this slug (e.g. ``bsvibe-app``).",
    )
    selector.add_argument(
        "--workspace-id",
        type=uuid.UUID,
        help="Retrofit every complete-bootstrap product in this workspace.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log the workspace vaults that would be touched without mutating them.",
    )
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    """Async body of :func:`main` — opens its own engine so the CLI is self-contained."""
    settings = get_settings()
    engine = make_engine()
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        vault_root = Path(settings.knowledge_vault_root)
        processed = await run_backfill(
            session_factory=session_factory,
            product_slug=args.product_slug,
            workspace_id=args.workspace_id,
            vault_root=vault_root,
            dry_run=bool(args.dry_run),
        )
    finally:
        await engine.dispose()
    return 0 if processed > 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging(level="INFO", service_name="bsvibe-anchor-backfill")
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "run_backfill"]
