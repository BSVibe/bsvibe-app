"""Where a workspace's vault lives. THE definition — there must not be a second.

The layout is ``<knowledge_vault_root>/<region>/<workspace_id>/``. ``region``
used to be a per-workspace column, and its ONLY effect was this path segment:
nothing routed, sharded, or enforced data residency on it. Two answers existed
anyway — the writers (settle worker, MCP tools, bootstrap) read
``WorkspaceRow.region`` while every REST knowledge route used the deployment
default — and the split was invisible because prod has one region and it IS the
default (3/3 workspaces on ``us-1``).

Invisible, but reachable: the API accepted an arbitrary ``region`` on create and
on patch. One field away from the default, the REST knowledge surface would read
an empty directory while the settle hook wrote to another — concepts empty,
graph empty, reindex reporting ``scanned: 0`` as a success.

So region is a DEPLOYMENT constant, not a per-workspace value. The repo had
already retired a sibling axis on the same grounds
(``20260824_drop_data_jurisdiction.py`` — "강제된 적 없는 축을 지운다").
"""

from __future__ import annotations

import uuid
from pathlib import Path

from backend.config import get_settings


def workspace_vault_root(workspace_id: uuid.UUID) -> Path:
    """``<knowledge_vault_root>/<region>/<workspace_id>/`` for one workspace.

    Pure and synchronous ON PURPOSE: the moment this needs a DB read, some
    caller is asking a per-workspace question about a deployment constant, and
    a second answer has been reintroduced.
    """
    settings = get_settings()
    return (
        Path(settings.knowledge_vault_root) / settings.knowledge_default_region / str(workspace_id)
    )


__all__ = ["workspace_vault_root"]
