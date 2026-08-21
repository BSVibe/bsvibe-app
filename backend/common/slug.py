"""자유 문자열 → 안전한 슬러그. **경로 탈출 방어가 사는 곳이다.**

이 함수는 원래 ``backend/api/v1/skills.py`` 와 ``backend/mcp/tools/skills_tools.py``
**양쪽에** 있었다. MCP 쪽이 그 이유를 스스로 적어뒀다 — *"Kept local so the MCP
module doesn't reach into the forbidden ``backend.api`` subtree. … Mirrors
``backend.api.v1.skills._slugify`` 1:1."*

**계약이 보안 방어의 사본을 강제한 셈이다.** 한쪽만 고쳐지면 다른 표면은 뚫린 채로
남는다. ``backend.common`` 은 아무것도 import 하지 않는 leaf 라 두 표면 모두
계약을 깨지 않고 의존할 수 있다 — :mod:`backend.common.settle_kinds` 가 같은
이유로 여기 있다.
"""

from __future__ import annotations

import re

#: 만들어진 스킬의 파일명 + manifest name 이 따르는 문법 (로더가 ``SkillMeta`` 로
#: 같은 것을 강제한다).
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")

__all__ = ["SLUG_RE", "slugify"]


def slugify(name: str) -> str | None:
    """Derive a safe ``^[a-z][a-z0-9-]*$`` slug from a free-form name.

    Returns ``None`` when the name cannot yield a safe slug — including any name
    carrying a path separator or ``..`` (path-traversal defense: a created skill
    MUST stay inside the per-workspace dir, so we never derive a slug from a name
    that looks like a path).
    """
    if "/" in name or "\\" in name or ".." in name:
        return None
    # Lowercase; collapse any run of non-[a-z0-9] into a single hyphen.
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug or not SLUG_RE.match(slug):
        return None
    return slug
