"""_WriterMutationMixin — operations that modify, delete, or promote existing notes.

Extracted from the original monolithic ``writer_core.py`` during Lift L1
(v8 §17.3). Behaviour is identical to the pre-decomp implementation; only
the file boundary changed.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from backend.knowledge._internal.events import emit_event
from backend.knowledge.graph.note import build_frontmatter
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer_core._entity_stub import _maturity_from_status

if TYPE_CHECKING:
    from backend.knowledge._internal.events import EventBus

    # TODO(bundle-k-integration): out-of-scope source dep -- original: from bsage.core.skill_context import GraphInterface
    GraphInterface = Any
    # TODO(bundle-k-integration): wire to plugin.audit -- original: from bsage.garden.audit_outbox import AiosqliteAuditOutbox
    AiosqliteAuditOutbox = Any

logger = structlog.get_logger(__name__)


class _WriterMutationMixin:
    """Operations that modify, delete, or promote existing notes."""

    _vault: Vault
    _event_bus: EventBus | None
    _audit_outbox: AiosqliteAuditOutbox | None
    _default_tenant_id: str | None
    # Provided by _WriterIOMixin once composed into GardenWriter.
    _garden_lock: asyncio.Lock

    @staticmethod
    def _find_dedup_path(directory: Path, slug: str) -> Path:  # pragma: no cover - in GardenWriter
        raise NotImplementedError

    async def _notify_sync(  # pragma: no cover - in GardenWriter
        self, event_type_str: str, path: Path, source: str
    ) -> None: ...

    async def _emit_vault_modified(  # pragma: no cover - in GardenWriter
        self,
        *,
        path: Path,
        operation: str,
        source: str,
        note_type: str | None = None,
        tenant_id: str | None = None,
    ) -> None: ...

    # Re-exposed pre-compiled regex (kept on the class for backwards
    # compatibility with any external code that introspected ``_STATUS_RE``).
    _STATUS_RE = re.compile(r"^(status:\s*)\S+", re.MULTILINE)

    async def update_frontmatter_status(self, note_path: Path, new_status: str) -> None:
        """Update the ``status`` field in a note's YAML frontmatter in-place."""
        content = await asyncio.to_thread(note_path.read_text, "utf-8")
        updated = self._STATUS_RE.sub(rf"\g<1>{new_status}", content, count=1)
        if updated == content:
            return
        await asyncio.to_thread(note_path.write_text, updated, encoding="utf-8")
        rel_path = str(note_path.relative_to(self._vault.root))
        await emit_event(
            self._event_bus,
            "NOTE_UPDATED",
            {"path": rel_path, "field": "status", "new_value": new_status},
        )
        logger.info("maturity_status_updated", path=rel_path, new_status=new_status)

    async def promote_maturity(
        self,
        graph: GraphInterface | None,
        config: Any = None,
    ) -> dict[str, Any]:
        """Scan all garden notes and promote eligible ones."""
        from backend.knowledge.graph.markdown_utils import extract_frontmatter
        from backend.knowledge.retrieval.maturity import MaturityConfig, MaturityEvaluator

        if graph is None:
            return {"promoted": 0, "checked": 0, "details": []}

        if config is None:
            config = MaturityConfig()

        evaluator = MaturityEvaluator(graph, config)

        # Scan garden/seedling+budding+evergreen plus non-system top-level dirs.
        legacy_skip = {"seeds", "actions", "tmp", "node_modules", "garden", ".bsage"}

        def _collect_md() -> list[Path]:
            files: list[Path] = []
            seen: set[Path] = set()

            def _add(base: Path) -> None:
                resolved = base.resolve()
                if resolved in seen:
                    return
                seen.add(resolved)
                if base.is_dir():
                    files.extend(base.rglob("*.md"))

            garden_root = self._vault.root / "garden"
            if garden_root.is_dir():
                for child in garden_root.iterdir():
                    if child.is_dir() and not child.name.startswith((".", "_")):
                        _add(child)

            for child in sorted(self._vault.root.iterdir()):
                if not child.is_dir() or child.name.startswith((".", "_")):
                    continue
                if child.name in legacy_skip:
                    continue
                _add(child)

            return sorted(files)

        md_files = await asyncio.to_thread(_collect_md)
        promoted = 0
        details: list[dict[str, str]] = []

        for md_file in md_files:
            rel_path = str(md_file.relative_to(self._vault.root))
            content = await asyncio.to_thread(md_file.read_text, "utf-8")
            fm = extract_frontmatter(content)
            current_status = fm.get("status", "seed")
            current_maturity = fm.get("maturity") or _maturity_from_status(current_status)

            new_status = await evaluator.evaluate(rel_path, current_status)
            if new_status is None:
                continue

            target_maturity = new_status.value
            new_path = await self._apply_maturity_promotion(
                md_file=md_file,
                target_maturity=target_maturity,
                current_maturity=current_maturity,
            )
            promoted += 1
            details.append(
                {
                    "path": str(new_path.relative_to(self._vault.root)),
                    "from": current_status,
                    "to": target_maturity,
                }
            )
            logger.info(
                "note_promoted",
                path=rel_path,
                from_status=current_status,
                to_status=target_maturity,
                new_path=str(new_path.relative_to(self._vault.root)),
            )

        return {"promoted": promoted, "checked": len(md_files), "details": details}

    async def _apply_maturity_promotion(
        self, *, md_file: Path, target_maturity: str, current_maturity: str
    ) -> Path:
        """Update frontmatter ``status`` + ``maturity`` and move file when the
        target folder changes. Returns the (possibly new) path."""
        await self.update_frontmatter_status(md_file, target_maturity)
        await self._set_frontmatter_field(md_file, "maturity", target_maturity)

        if target_maturity == current_maturity:
            return md_file
        if not str(md_file).startswith(str(self._vault.root)):
            return md_file
        rel = md_file.relative_to(self._vault.root)
        # Only auto-move notes that already live in the maturity tree.
        if not str(rel).startswith("garden/"):
            return md_file

        target_dir = self._vault.resolve_path(f"garden/{target_maturity}")
        target_dir.mkdir(parents=True, exist_ok=True)
        new_path = target_dir / md_file.name
        if new_path == md_file:
            return md_file
        if new_path.exists():
            new_path = self._find_dedup_path(target_dir, md_file.stem)
        await asyncio.to_thread(md_file.rename, new_path)
        return new_path

    async def _set_frontmatter_field(self, path: Path, key: str, value: Any) -> None:
        """Set a single frontmatter field, preserving body and other fields."""
        async with self._garden_lock:
            text = await asyncio.to_thread(path.read_text, "utf-8")
            if not text.startswith("---\n"):
                fm = {key: value}
                new_text = build_frontmatter(fm) + text
                await asyncio.to_thread(path.write_text, new_text, encoding="utf-8")
                return
            closing = text.find("\n---\n", 4)
            if closing == -1:
                msg = (
                    f"malformed frontmatter in {path}: opening '---' present "
                    f"but no closing '---' found — refusing to silently rewrite"
                )
                raise ValueError(msg)
            try:
                fm = yaml.safe_load(text[4:closing]) or {}
            except yaml.YAMLError as exc:
                msg = f"corrupted YAML frontmatter in {path}: {exc}"
                raise ValueError(msg) from exc
            if not isinstance(fm, dict):
                msg = (
                    f"frontmatter root in {path} is {type(fm).__name__}, "
                    f"expected dict — cannot set field {key!r}"
                )
                raise ValueError(msg)
            fm[key] = value
            new_text = build_frontmatter(fm) + text[closing + 5 :]
            await asyncio.to_thread(path.write_text, new_text, encoding="utf-8")

    async def update_note(
        self, path: str, content: str, *, preserve_frontmatter: bool = True
    ) -> Path:
        """Replace the content of an existing vault note."""
        resolved = self._vault.resolve_path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Note not found: {path}")

        if preserve_frontmatter:
            existing = await self._vault.read_note_content(resolved)
            if existing.startswith("---\n"):
                try:
                    end_idx = existing.index("\n---\n", 4)
                except ValueError as exc:
                    msg = (
                        f"cannot preserve frontmatter for {path}: opening "
                        f"'---' present but no closing '---' found"
                    )
                    raise ValueError(msg) from exc
                frontmatter = existing[: end_idx + 5]
                content = frontmatter + "\n" + content

        await asyncio.to_thread(resolved.write_text, content, encoding="utf-8")
        logger.info("note_updated", path=str(resolved))
        await self._notify_sync("garden", resolved, "update")
        await emit_event(self._event_bus, "NOTE_UPDATED", {"path": str(resolved)})
        await self._emit_vault_modified(
            path=resolved,
            operation="note_updated",
            source="update",
        )
        return resolved

    async def update_frontmatter_related(self, note_path: str, linked_paths: set[str]) -> None:
        """Merge auto-discovered links into the note's frontmatter ``related`` field."""
        try:
            abs_path = self._vault.resolve_path(note_path)
            if not abs_path.resolve().is_relative_to(self._vault.root.resolve()):
                logger.warning("path_traversal_blocked", note_path=note_path)
                raise ValueError(f"Path traversal blocked: {note_path}")
            if not abs_path.exists():
                return
            content = await self._vault.read_note_content(abs_path)
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return

        if not content.startswith("---\n"):
            return
        try:
            end_idx = content.index("\n---\n", 4)
        except ValueError:
            return

        fm_str = content[4:end_idx]
        body = content[end_idx + 5 :]

        try:
            metadata = yaml.safe_load(fm_str)
        except (yaml.YAMLError, ValueError):
            return
        if not isinstance(metadata, dict):
            return

        new_links = {f"[[{Path(lp).stem}]]" for lp in linked_paths}
        existing_related = metadata.get("related", [])
        existing_set = set(existing_related) if isinstance(existing_related, list) else set()
        merged = sorted(existing_set | new_links)
        if merged == sorted(existing_set):
            return

        metadata["related"] = merged
        new_fm = build_frontmatter(metadata)
        new_content = f"{new_fm}\n{body}"
        await asyncio.to_thread(abs_path.write_text, new_content, encoding="utf-8")
        logger.debug("note_related_updated", note_path=note_path, links=len(merged))
        await self._notify_sync("garden", abs_path, "update")
        await emit_event(self._event_bus, "NOTE_UPDATED", {"path": note_path})

    async def append_to_note(self, path: str, text: str) -> Path:
        """Append text to an existing vault note."""
        resolved = self._vault.resolve_path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Note not found: {path}")

        def _append() -> None:
            with resolved.open("a", encoding="utf-8") as f:
                f.write(text)

        await asyncio.to_thread(_append)
        logger.info("note_appended", path=str(resolved))
        await self._notify_sync("garden", resolved, "update")
        await emit_event(self._event_bus, "NOTE_UPDATED", {"path": str(resolved)})
        await self._emit_vault_modified(
            path=resolved,
            operation="note_appended",
            source="update",
        )
        return resolved

    async def delete_note(self, path: str) -> None:
        """Delete a note from the vault.

        Raises ValueError if path is in ``actions/`` (action logs are append-only)
        or escapes the vault boundary.
        """
        if path.startswith("actions/"):
            raise ValueError("Cannot delete action logs")
        resolved = self._vault.resolve_path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Note not found: {path}")

        await asyncio.to_thread(resolved.unlink)
        logger.info("note_deleted", path=str(resolved))
        await self._notify_sync("garden", resolved, "delete")
        await emit_event(self._event_bus, "NOTE_DELETED", {"path": str(resolved)})
        await self._emit_vault_modified(
            path=resolved,
            operation="note_deleted",
            source="delete",
        )
