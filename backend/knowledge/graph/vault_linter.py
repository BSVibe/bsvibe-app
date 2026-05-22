"""VaultLinter — unified vault health check (Karpathy Wiki lint operation).

Scans all garden notes for:
- Orphan pages (no related links)
- Stale notes (old captured_at, no updates)
- Missing frontmatter fields

Writes a lint report as a garden insight note.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from backend.knowledge.graph.markdown_utils import extract_frontmatter, extract_title
from backend.knowledge.graph.writer import GardenNote

if TYPE_CHECKING:
    from backend.knowledge.graph.vault import Vault
    from backend.knowledge.graph.writer import GardenWriter
    from backend.knowledge.retrieval.ontology import OntologyRegistry

logger = structlog.get_logger(__name__)


@dataclass
class LintIssue:
    """A single lint issue found during vault health check."""

    check: str  # orphan, stale, missing_field
    severity: str  # warning, error
    path: str
    description: str


@dataclass
class LintReport:
    """Result of a vault lint operation."""

    issues: list[LintIssue] = field(default_factory=list)
    total_notes_scanned: int = 0
    timestamp: str = field(
        default_factory=lambda: datetime.now(tz=UTC).isoformat(timespec="seconds"),
    )


class VaultLinter:
    """Performs comprehensive vault health checks."""

    def __init__(
        self,
        vault: Vault,
        garden_writer: GardenWriter,
        graph_store: Any | None = None,
        ontology: OntologyRegistry | None = None,
        stale_days: int = 90,
    ) -> None:
        self._vault = vault
        self._writer = garden_writer
        self._graph_store = graph_store
        self._ontology = ontology
        self._stale_days = stale_days

    def _resolve_garden_dirs(self) -> list[str]:
        """Garden directories the linter scans.

        Walks the maturity tree (``garden/seedling``, ``garden/budding``,
        ``garden/evergreen``) plus the ``garden/entities`` stub folder.
        Unmigrated legacy folders (``ideas/``, ``insights/``...) are
        picked up by walking the vault root so the linter still works
        on vaults that haven't run ``bsage migrate-flatten-vault`` yet.
        """
        primary = ["garden/seedling", "garden/budding", "garden/evergreen", "garden/entities"]
        merged: list[str] = []
        seen: set[str] = set()
        for entry in primary:
            if (self._vault.root / entry).exists() and entry not in seen:
                seen.add(entry)
                merged.append(entry)
        # Legacy fallback: any non-system top-level directory is a candidate.
        skip = {"seeds", "actions", "tmp", "node_modules", "garden"}
        for child in sorted(self._vault.root.iterdir()):
            if (
                child.is_dir()
                and not child.name.startswith((".", "_"))
                and child.name not in skip
                and child.name not in seen
            ):
                seen.add(child.name)
                merged.append(child.name)
        return merged

    async def lint(self) -> LintReport:
        """Run all lint checks and write a report note.

        Returns:
            LintReport with all issues found.
        """
        notes = await self._scan_all_notes()
        report = LintReport(total_notes_scanned=len(notes))

        if not notes:
            return report

        # Run checks
        report.issues.extend(self._check_orphans(notes))
        report.issues.extend(self._check_stale(notes))

        # Write report as garden insight
        await self._write_report(report)

        logger.info(
            "vault_lint_complete",
            notes_scanned=report.total_notes_scanned,
            issues_found=len(report.issues),
        )
        return report

    async def _scan_all_notes(self) -> list[dict[str, Any]]:
        """Scan all garden notes and return parsed metadata."""
        notes: list[dict[str, Any]] = []
        garden_dirs = self._resolve_garden_dirs()

        for subdir in garden_dirs:
            dir_path = self._vault.root / subdir
            if not dir_path.is_dir():
                continue

            def _list_md(d: Path = dir_path) -> list[Path]:
                return sorted(d.glob("*.md"))

            md_files = await asyncio.to_thread(_list_md)
            for abs_path in md_files:
                try:
                    content = await self._vault.read_note_content(abs_path)
                    fm = extract_frontmatter(content)
                    title = fm.get("title", "") or extract_title(content) or abs_path.stem
                    rel_path = str(abs_path.relative_to(self._vault.root))
                    notes.append(
                        {
                            "path": rel_path,
                            "title": title,
                            "frontmatter": fm,
                            "content": content,
                        }
                    )
                except (FileNotFoundError, OSError, UnicodeDecodeError):
                    continue

        return notes

    def _check_orphans(self, notes: list[dict[str, Any]]) -> list[LintIssue]:
        """Find notes with no related links (orphans)."""
        issues: list[LintIssue] = []
        for note in notes:
            fm = note["frontmatter"]
            related = fm.get("related", [])
            if not related:
                issues.append(
                    LintIssue(
                        check="orphan",
                        severity="warning",
                        path=note["path"],
                        description=f"'{note['title']}' has no related links (orphan page)",
                    )
                )
        return issues

    def _check_stale(self, notes: list[dict[str, Any]]) -> list[LintIssue]:
        """Find notes with old captured_at dates."""
        issues: list[LintIssue] = []
        now = datetime.now(tz=UTC)

        for note in notes:
            fm = note["frontmatter"]
            captured = fm.get("captured_at", "")
            if not captured:
                continue
            try:
                captured_date = datetime.strptime(str(captured), "%Y-%m-%d").replace(tzinfo=UTC)
                days_old = (now - captured_date).days
                if days_old > self._stale_days:
                    issues.append(
                        LintIssue(
                            check="stale",
                            severity="warning",
                            path=note["path"],
                            description=(
                                f"'{note['title']}' captured {days_old} days ago "
                                f"(threshold: {self._stale_days} days)"
                            ),
                        )
                    )
            except (ValueError, TypeError):
                continue

        return issues

    async def _write_report(self, report: LintReport) -> None:
        """Write lint report as a garden insight note."""
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        lines = [f"# Vault Lint Report — {date_str}\n"]
        lines.append(
            f"Scanned **{report.total_notes_scanned}** notes. "
            f"Found **{len(report.issues)}** issues.\n"
        )

        if not report.issues:
            lines.append("No issues found. Vault is healthy.\n")
        else:
            # Group by check type
            by_check: dict[str, list[LintIssue]] = {}
            for issue in report.issues:
                by_check.setdefault(issue.check, []).append(issue)

            for check_name, issues in sorted(by_check.items()):
                lines.append(f"## {check_name.title()} ({len(issues)})\n")
                for issue in issues[:20]:  # Cap per section
                    lines.append(f"- [{issue.severity}] {issue.description}")
                if len(issues) > 20:
                    lines.append(f"- ... and {len(issues) - 20} more")
                lines.append("")

        content = "\n".join(lines)

        await self._writer.write_garden(
            GardenNote(
                title=f"Vault Lint Report {date_str}",
                content=content,
                note_type="insight",
                source="vault-linter",
                tags=["lint", "health-check"],
            )
        )
