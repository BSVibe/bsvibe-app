"""Skills module — Workflow §6 #5 [locked] format.

Public surface:

* :class:`SkillMeta` — parsed manifest (name / version / description +
  optional author / allowed_tools / model + Markdown body)
* :class:`SkillLoader` — per-workspace ``skills/<workspace_id>/*.md`` discovery
* :func:`invoke_skill` — runtime entry point; consumed as a tool by the
  agent loop (Bundle X)
* :class:`SkillLoadError`, :class:`SkillRunError`
"""

from __future__ import annotations

from backend.extensions.skill.exceptions import SkillError, SkillLoadError, SkillRunError
from backend.extensions.skill.loader import SkillLoader
from backend.extensions.skill.meta import SkillMeta
from backend.extensions.skill.runner import (
    CompletionFn,
    Searcher,
    SkillRunResult,
    invoke_skill,
)

__all__ = [
    "CompletionFn",
    "Searcher",
    "SkillError",
    "SkillLoadError",
    "SkillLoader",
    "SkillMeta",
    "SkillRunError",
    "SkillRunResult",
    "invoke_skill",
]
