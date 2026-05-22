"""SkillLoader — discover + parse ``skills/<workspace_id>/*.md`` per Workflow §6 #5."""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml

from backend.skills.exceptions import SkillLoadError
from backend.skills.meta import (
    ALLOWED_FRONTMATTER_FIELDS,
    DROPPED_FRONTMATTER_FIELDS,
    REQUIRED_FRONTMATTER_FIELDS,
    SkillMeta,
)

logger = structlog.get_logger(__name__)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter_yaml, body)``. Empty frontmatter when no fences."""
    if not text.startswith("---\n"):
        return "", text
    try:
        end_idx = text.index("\n---\n", 4)
    except ValueError:
        return "", text
    return text[4:end_idx], text[end_idx + 5 :]


class SkillLoader:
    """Scan a per-workspace skills directory for ``*.md`` skill manifests.

    The factory layer (``KnowledgeFactory``-style) is expected to construct
    one loader per workspace with ``skill_dir = skills/<workspace_id>/``.
    """

    def __init__(self, skill_dir: Path) -> None:
        self._skill_dir = skill_dir
        self._registry: dict[str, SkillMeta] = {}

    @property
    def registry(self) -> dict[str, SkillMeta]:
        return dict(self._registry)

    def load_all(self) -> dict[str, SkillMeta]:
        """Reload every ``*.md`` in ``skill_dir``. Replaces existing registry."""
        self._registry.clear()
        if not self._skill_dir.is_dir():
            logger.warning("skill_dir_missing", path=str(self._skill_dir))
            return {}
        for md_path in sorted(self._skill_dir.glob("*.md")):
            if not md_path.is_file():
                continue
            try:
                meta = self._parse(md_path)
            except SkillLoadError as exc:
                logger.warning("skill_load_failed", path=str(md_path), error=str(exc))
                continue
            self._registry[meta.name] = meta
            logger.info("skill_loaded", name=meta.name, version=meta.version)
        return dict(self._registry)

    def get(self, name: str) -> SkillMeta:
        if name not in self._registry:
            raise SkillLoadError(f"Skill '{name}' not in registry.")
        return self._registry[name]

    @staticmethod
    def _parse(path: Path) -> SkillMeta:
        text = path.read_text(encoding="utf-8")
        frontmatter_str, body = _split_frontmatter(text)
        if not frontmatter_str:
            raise SkillLoadError(f"No YAML frontmatter in {path}")
        try:
            data = yaml.safe_load(frontmatter_str)
        except yaml.YAMLError as exc:
            raise SkillLoadError(f"Malformed frontmatter in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise SkillLoadError(f"Invalid frontmatter structure in {path}")

        # Reject pre-Workflow-§6-#5 fields with a pointer to the removed key.
        dropped_present = DROPPED_FRONTMATTER_FIELDS & set(data.keys())
        if dropped_present:
            raise SkillLoadError(
                f"{path}: removed frontmatter fields {sorted(dropped_present)}. "
                "Per Workflow §6 #5 [locked], skills no longer carry category / "
                "trigger / read_context / output_target / output_format / credentials."
            )

        missing = REQUIRED_FRONTMATTER_FIELDS - set(data.keys())
        if missing:
            raise SkillLoadError(f"{path}: missing required fields {sorted(missing)}")

        # Silently drop unknown keys — forward-compat for future Workflow expansions.
        filtered = {k: v for k, v in data.items() if k in ALLOWED_FRONTMATTER_FIELDS}

        body_stripped = body.strip()
        if body_stripped:
            filtered["system_prompt"] = body_stripped

        return SkillMeta(**filtered)
