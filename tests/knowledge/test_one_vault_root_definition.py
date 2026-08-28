"""Every surface must resolve a workspace's vault to the SAME directory.

``region`` was a per-workspace column whose only effect was a path segment:
``<knowledge_vault_root>/<region>/<workspace_id>/``. Nothing routed, sharded or
enforced residency on it — measured across the backend, its only consumers were
that path and the plumbing that carried it there.

Two answers existed anyway. The writers (settle worker, MCP tools, bootstrap)
read ``WorkspaceRow.region``; every REST knowledge route used the deployment
default. Nobody noticed, because prod has one region and it IS the default —
3/3 workspaces on ``us-1``. But the API accepts an arbitrary ``region`` on
create AND on patch, so a workspace one field away from the default would have
had its whole REST knowledge surface reading an empty directory while the
settle hook wrote to another: concepts empty, graph empty, reindex reporting
``scanned: 0`` as a success.

The repo had already deleted a sibling axis for the same reason —
``20260824_drop_data_jurisdiction.py``: "강제된 적 없는 축을 지운다". Region is
that shape. So the fix is not to teach REST the second answer; it is to stop
having two. This test pins that there is exactly one.
"""

from __future__ import annotations

import uuid

from backend.config import get_settings
from backend.knowledge.graph.vault_paths import workspace_vault_root


def test_the_vault_root_does_not_depend_on_anything_per_workspace() -> None:
    """One workspace id → one path, derived from deployment settings alone."""
    workspace_id = uuid.uuid4()
    settings = get_settings()

    root = workspace_vault_root(workspace_id)

    assert root.parts[-1] == str(workspace_id)
    assert root.parts[-2] == settings.knowledge_default_region
    assert str(root).startswith(settings.knowledge_vault_root)


def test_repeated_resolution_is_stable() -> None:
    """No hidden state — the same id resolves the same way every time."""
    workspace_id = uuid.uuid4()

    assert workspace_vault_root(workspace_id) == workspace_vault_root(workspace_id)


# The absence half of this file's proposition — "no surface resolves a vault
# through a per-workspace region" — moved to
# ``tests/test_the_workspace_region_axis_is_gone.py`` when the column itself was
# dropped. That guard is strictly stronger: it walks the AST instead of grepping
# text (so docstrings explaining the removal cannot satisfy or trip it) and it
# pins a file SET rather than a list of spellings. Keeping a weaker copy here
# would be a mirrored surface, and mirrored surfaces drift toward whichever side
# is tested less.
