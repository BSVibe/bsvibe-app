"""Workspace → delivery binding resolution (Lift §17.7).

Two resolvers:

* :func:`_resolve_bindings` — every active ``connector_accounts`` row whose
  connector has a v1 event builder AND a non-empty ``delivery_config``. These
  are the simple-event-builder bindings (notion / slack / email-sender /
  telegram / discord / linear / trello).
* :func:`resolve_github_binding` — the github special case (NOT a simple event
  builder — it needs git-ops, not just an event dict). Used by both the
  delivery adapter AND the run-setup workspace provisioner that clones the
  github target.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.base import PluginMeta

from ._builders import OUTBOUND_EVENT_BUILDERS, OutboundEventBuilder

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class _Binding:
    account: ConnectorAccountRow
    plugin: PluginMeta
    builder: OutboundEventBuilder


async def _resolve_bindings(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    plugins_by_name: dict[str, PluginMeta],
) -> list[_Binding]:
    """Active connector_accounts for the workspace that are deliverable targets.

    A row qualifies when ALL hold: it is ``is_active``, its ``delivery_config``
    is non-empty, its ``connector`` has a loaded plugin that declares at least
    one ``@p.outbound``, AND a v1 event-builder exists for that connector. Rows
    failing any condition are skipped (the others without a builder are the
    deliberate seam for connectors not yet wired).
    """
    rows = (
        (
            await session.execute(
                select(ConnectorAccountRow).where(
                    ConnectorAccountRow.workspace_id == workspace_id,
                    ConnectorAccountRow.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    bindings: list[_Binding] = []
    for row in rows:
        if not row.delivery_config:
            continue
        plugin = plugins_by_name.get(row.connector)
        if plugin is None or not plugin.outbounds:
            continue
        builder = OUTBOUND_EVENT_BUILDERS.get(row.connector)
        if builder is None:
            logger.info(
                "connector_delivery_no_builder_skipped",
                connector=row.connector,
                workspace_id=str(workspace_id),
            )
            continue
        bindings.append(_Binding(account=row, plugin=plugin, builder=builder))
    return bindings


@dataclass(slots=True)
class GithubBinding:
    """A workspace's github delivery target: the account + its ``repo`` config.

    ``repo`` is the founder-set ``delivery_config['repo']`` (``owner/name``);
    ``base_branch`` is ``delivery_config['base_branch']`` (default ``main``). The
    github connector's encrypted secret IS the git push / API token (the same
    secret slot the inbound webhook uses — connectors reuse the one stored
    secret).
    """

    account: ConnectorAccountRow
    repo: str
    base_branch: str


async def resolve_github_binding(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> GithubBinding | None:
    """The workspace's active github delivery target, or ``None``.

    Mirrors :func:`_resolve_bindings` but for the github special case (github is
    NOT a simple event builder — it needs git-ops, not just an event dict). A
    row qualifies when it is ``is_active``, its ``connector`` is ``github``, and
    its ``delivery_config`` carries a non-empty ``repo``. The first such row
    wins (a workspace has one github delivery target in v1).
    """
    rows = (
        (
            await session.execute(
                select(ConnectorAccountRow).where(
                    ConnectorAccountRow.workspace_id == workspace_id,
                    ConnectorAccountRow.connector == "github",
                    ConnectorAccountRow.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        repo = (row.delivery_config or {}).get("repo")
        if not repo:
            continue
        base_branch = str((row.delivery_config or {}).get("base_branch") or "main")
        return GithubBinding(account=row, repo=str(repo), base_branch=base_branch)
    return None


__all__ = [
    "GithubBinding",
    "_Binding",
    "_resolve_bindings",
    "resolve_github_binding",
]
