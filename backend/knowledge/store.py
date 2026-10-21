"""Move the vault and skills between disk and ``vault_documents`` rows (#929, B — stage 2).

``python -m backend.knowledge.store import [--dry-run]`` copies every workspace's vault
(``<knowledge_vault_root>/<region>/<workspace>/``) and skills (``<skills_root>/<workspace>/``)
into rows — byte for byte, at the same relative paths. Run it before flipping
``knowledge_store_backend`` to ``postgres``; it is safe to run again (an unchanged document is
not rewritten). ``python -m backend.knowledge.store export <dir>`` writes the rows back out as
the same tree. docs/design/stateless-workers.md §5 and docs/runbook/vault-to-postgres.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from backend.config import get_settings
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.infrastructure.document_store import PgDocumentStorage, VaultDocumentRow

logger = structlog.get_logger(__name__)


@dataclass
class ImportReport:
    written: int = 0
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)


def _workspace_dirs(base: Path) -> list[tuple[uuid.UUID, Path]]:
    """``(workspace_id, dir)`` for every child of ``base`` named like a workspace."""
    if not base.is_dir():
        return []
    found = []
    for child in sorted(base.iterdir()):
        try:
            ws = uuid.UUID(child.name)
        except ValueError:
            logger.warning("store_import_not_a_workspace", path=str(child))
            continue
        if child.is_dir():
            found.append((ws, child))
    return found


def _disk_trees() -> list[tuple[uuid.UUID, str, Path]]:
    s = get_settings()
    vault_base = Path(s.knowledge_vault_root) / s.knowledge_default_region
    return [(ws, "vault", d) for ws, d in _workspace_dirs(vault_base)] + [
        (ws, "skills", d) for ws, d in _workspace_dirs(Path(s.skills_root))
    ]


async def import_from_disk(session_factory: Any, *, dry_run: bool = False) -> ImportReport:
    """Copy every workspace's vault and skills from disk into rows."""
    report = ImportReport()
    for ws, tree, root in _disk_trees():
        disk = FileSystemStorage(root)
        rows = PgDocumentStorage(session_factory, workspace_id=ws, tree=tree)  # type: ignore[arg-type]
        paths = await disk.list_files("", "*")
        existing = await rows.read_many(paths)
        for rel in paths:
            try:
                content = await disk.read(rel)
            except UnicodeDecodeError:
                report.skipped.append(f"{tree}/{ws}/{rel}")
                continue
            if existing.get(rel) == content:
                report.unchanged += 1
                continue
            if not dry_run:
                await rows.write(rel, content)
            report.written += 1
    logger.info(
        "store_import_done",
        written=report.written,
        unchanged=report.unchanged,
        skipped=len(report.skipped),
        dry_run=dry_run,
    )
    return report


async def export_to_disk(session_factory: Any, out: Path) -> int:
    """Write every row back out as ``<out>/vault/<region>/<ws>/…`` and ``<out>/skills/<ws>/…``."""
    vault_out = out / "vault" / get_settings().knowledge_default_region
    # ``vault_documents`` is a layer-2 table (no RLS, like ``note_embeddings``): an unscoped
    # session sees every workspace's rows, which is what an export of all of them needs.
    async with session_factory() as session:
        trees = (
            await session.execute(
                select(VaultDocumentRow.workspace_id, VaultDocumentRow.tree).distinct()
            )
        ).all()
    count = 0
    for ws, tree in sorted(trees, key=lambda t: (str(t[0]), t[1])):
        rows = PgDocumentStorage(session_factory, workspace_id=ws, tree=tree)
        base = vault_out / str(ws) if tree == "vault" else out / tree / str(ws)
        disk = FileSystemStorage(base)
        documents = await rows.read_many(await rows.list_files("", "*"))
        for rel, content in documents.items():
            await disk.write(rel, content)
            count += 1
    logger.info("store_export_done", documents=count, out=str(out))
    return count


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.knowledge.store",
        description="Move the vault and skills between disk and vault_documents rows.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="disk → rows (safe to run again)")
    imp.add_argument("--dry-run", action="store_true", help="count, write nothing")
    exp = sub.add_parser("export", help="rows → a directory tree")
    exp.add_argument("out", type=Path)
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415

    from backend.data.engine import create_app_engine  # noqa: PLC0415

    engine = create_app_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.command == "import":
            report = await import_from_disk(session_factory, dry_run=args.dry_run)
            verb = "would write" if args.dry_run else "wrote"
            print(
                f"{verb} {report.written} · unchanged {report.unchanged} · skipped {len(report.skipped)}"
            )
            for where in report.skipped:
                print(f"  SKIPPED (not UTF-8) {where}")
            return 1 if report.skipped else 0
        count = await export_to_disk(session_factory, args.out)
        print(f"exported {count} documents to {args.out}")
        return 0
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main_async(_parse_args(argv if argv is not None else sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["ImportReport", "export_to_disk", "import_from_disk", "main"]
