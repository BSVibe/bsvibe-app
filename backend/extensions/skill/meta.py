"""SkillMeta — Workflow §6 #5 [locked] frontmatter format.

Verbatim per Workflow §6 #5:

* **Required**: ``name``, ``version``, ``description`` (the LLM invocation
  match signal — write richly).
* **Optional**: ``author``, ``allowed_tools``, ``model``.
* **Body**: the Markdown system prompt.

Dropped from BSage's earlier format (now redundant):

* ``category`` — input/process/output framework retired
* ``trigger`` — Plugin inbound / Schedule / Direct / Decision resolution
* ``read_context`` — BSage retrieval at verify-declaration handles GATHER
* ``output_target`` / ``output_format`` — agent loop's Deliver event
* ``credentials`` — skills don't call external systems (LLM + Vault only)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# Frontmatter keys that were valid pre-Workflow §6 #5 and are now rejected
# at load time. Re-emitting the key name in the error message points
# authors at the removed field directly.
DROPPED_FRONTMATTER_FIELDS: frozenset[str] = frozenset(
    {
        "category",
        "trigger",
        "read_context",
        "output_target",
        "output_note_type",
        "output_format",
        "credentials",
    }
)

REQUIRED_FRONTMATTER_FIELDS: frozenset[str] = frozenset({"name", "version", "description"})

ALLOWED_FRONTMATTER_FIELDS: frozenset[str] = REQUIRED_FRONTMATTER_FIELDS | {
    "author",
    "allowed_tools",
    "model",
}


@dataclass(slots=True)
class SkillMeta:
    """Parsed skill manifest per Workflow §6 #5."""

    name: str
    version: str
    description: str
    author: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    system_prompt: str = ""

    def __post_init__(self) -> None:
        # ``SkillLoadError`` lives in a sibling module; importing at top-level
        # would create a circular reference once the loader imports SkillMeta.
        from backend.extensions.skill.exceptions import SkillLoadError  # noqa: PLC0415

        if not _NAME_RE.match(self.name):
            raise SkillLoadError(
                f"Invalid skill name '{self.name}'. Use lowercase alphanumeric "
                "with hyphens (^[a-z][a-z0-9-]*$)."
            )
        if not self.version:
            raise SkillLoadError(f"Skill '{self.name}' missing version.")
        if not self.description:
            raise SkillLoadError(
                f"Skill '{self.name}' missing description — required as the LLM "
                "invocation match signal."
            )
