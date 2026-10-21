"""A workspace's skills — loaded, created and updated in one place (#929, B — stage 2).

Skills are ``<slug>.md`` manifests in the workspace's skills tree, which
``knowledge_store_backend`` places on disk or in ``vault_documents`` rows. The API writes them
and the worker reads them, so on more than one machine they must live in the shared store; this
module is the one door for the REST surface, the MCP tools and the agent worker.
"""

from __future__ import annotations

import json
import uuid

from backend.common.slug import slugify
from backend.extensions.skill.loader import SkillLoader
from backend.extensions.skill.meta import SkillMeta
from backend.knowledge.vault_store import workspace_skills_storage


class SkillInvalid(ValueError):
    """The name yields no usable slug, or the summary is blank."""


class SkillConflict(Exception):  # noqa: N818 — reads as the condition
    """A skill with that slug already exists."""


class SkillNotFound(Exception):  # noqa: N818 — reads as the condition
    """No skill with that name."""


def skill_markdown(
    *, name: str, description: str, system_prompt: str, version: str = "1.0.0"
) -> str:
    """A skill manifest in the loader's format."""
    return (
        "---\n"
        f"name: {name}\n"
        f"version: {version}\n"
        f"description: {json.dumps(description)}\n"
        "---\n"
        f"{system_prompt.strip()}\n"
    )


async def workspace_skill_loader(workspace_id: uuid.UUID) -> SkillLoader:
    """``workspace_id``'s skills, loaded from whichever store is configured."""
    loader = SkillLoader(workspace_skills_storage(workspace_id))
    await loader.load()
    return loader


async def create_workspace_skill(
    workspace_id: uuid.UUID, *, name: str, summary: str, system_prompt: str
) -> SkillMeta:
    """Create ``<slug>.md`` — atomically: an existing slug is refused, never overwritten."""
    slug = slugify(name)
    if slug is None:
        raise SkillInvalid("name must yield a slug matching ^[a-z][a-z0-9-]*$")
    if not summary.strip():
        raise SkillInvalid("summary must not be blank")
    text = skill_markdown(name=slug, description=summary.strip(), system_prompt=system_prompt)
    if not await workspace_skills_storage(workspace_id).create(f"{slug}.md", text):
        raise SkillConflict(f"Skill '{slug}' already exists")
    return (await workspace_skill_loader(workspace_id)).get(slug)


async def update_workspace_skill(
    workspace_id: uuid.UUID, *, name: str, summary: str, system_prompt: str
) -> SkillMeta:
    """Rewrite an existing skill's summary and body; its name and version stay."""
    loader = await workspace_skill_loader(workspace_id)
    if name not in loader.registry:
        raise SkillNotFound(f"Skill '{name}' not found")
    if not summary.strip():
        raise SkillInvalid("summary must not be blank")
    existing = loader.get(name)
    storage = workspace_skills_storage(workspace_id)
    if not await storage.exists(f"{name}.md"):
        raise SkillNotFound(f"Skill '{name}' not found")
    await storage.write(
        f"{name}.md",
        skill_markdown(
            name=existing.name,
            description=summary.strip(),
            system_prompt=system_prompt,
            version=existing.version,
        ),
    )
    return (await workspace_skill_loader(workspace_id)).get(name)


__all__ = [
    "SkillConflict",
    "SkillInvalid",
    "SkillNotFound",
    "create_workspace_skill",
    "skill_markdown",
    "update_workspace_skill",
    "workspace_skill_loader",
]
