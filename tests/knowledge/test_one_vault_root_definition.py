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


def test_no_surface_resolves_a_vault_through_a_per_workspace_region() -> None:
    """The absence guard: a SECOND definition is what caused the split.

    ``workspace_region()`` read ``WorkspaceRow.region`` and existed only to feed
    a vault path. While any caller still asks the DB "which region is this
    workspace in?", the two answers can drift apart again — silently, because
    they agree on every workspace that exists today.
    """
    import subprocess
    from pathlib import Path

    from backend.knowledge.graph import vault_paths
    from backend.mcp.tools import _helpers

    # The symbol itself must be gone, not merely unused.
    assert not hasattr(_helpers, "workspace_region")

    # CODE-shaped patterns only. Grepping the bare name also matches the
    # docstrings that explain why it was removed — the "이름 충돌 / docstring
    # 언급" failure that makes an audit grep report work that was already done.
    # backend/knowledge/graph/vault_paths.py → parents[3] is the repo root;
    # getting it wrong makes this search a directory that does not exist and the
    # guard passes vacuously, which an absence test must never be allowed to do.
    repo = Path(vault_paths.__file__).resolve().parents[3]
    assert (repo / "backend" / "mcp").is_dir(), f"guard is searching nothing: {repo}"

    # An earlier version of this guard enumerated the receiver spellings it had
    # seen — ``row.region``, ``WorkspaceRow.region,`` — and passed while TWO live
    # readers survived under names it had not thought of (``ws.region`` in the
    # bootstrap runtime, ``target.region`` in the anchor-backfill CLI). A guard
    # built from a list of patterns only proves the author's imagination.
    #
    # So pin the FILE SET instead. Every code-shaped ``<x>.region`` read in the
    # backend is listed below with why it is allowed; a new reader anywhere —
    # under any spelling — fails this test because its file is not in the map.
    hits = subprocess.run(  # noqa: S603
        [
            "grep",
            "-rnE",
            "--include=*.py",
            r"[A-Za-z_][A-Za-z0-9_]*\.region\b",
            str(repo / "backend"),
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()

    # ``\`\``` filters prose: the docstrings explaining why the column was
    # retired mention it by name, and matching those is the "audit grep reports
    # work already done" failure.
    files = {
        line.split(":", 1)[0].replace(str(repo) + "/", "")
        for line in hits.splitlines()
        if "``" not in line
    }

    allowed = {
        # DISCLOSURE, not routing: both report what the row stores, to a caller
        # who asked what the row stores. Neither builds a path from it.
        "backend/api/v1/workspace_compliance.py",
        "backend/mcp/tools/account_tools.py",
        # WRITE side of the still-present column (the drop migration is a
        # separate change): persists what the API was given.
        "backend/api/v1/workspaces.py",
        # CARRIERS, not lookups: ``settlement.region`` / ``policy.region`` /
        # ``request.region`` are dataclass fields whose value originates from
        # ``settings.knowledge_default_region``. mypy pins their types, so none
        # of them can be a ``WorkspaceRow``. The plumbing that threads them is
        # removed with the column.
        "backend/knowledge/infrastructure/workers/settle_worker.py",
        "backend/workflow/application/runtime/settle_runtime.py",
        "backend/workflow/application/runtime/product_bootstrap_runtime.py",
    }

    survivors = sorted(files - allowed)
    assert not survivors, "a new per-workspace region reader appeared:\n" + "\n".join(survivors)

    # And the allowlist must not rot into a claim about files that no longer
    # read it — a stale entry would hide the next real reader in that file.
    assert not sorted(allowed - files), "allowlist names files that no longer read .region: " + str(
        sorted(allowed - files)
    )
