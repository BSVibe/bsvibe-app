"""Workspace → delivery binding resolution (Lift §17.7).

Two resolvers:

* :func:`_resolve_bindings` — an active ``connector_accounts`` row is a
  deliverable-delivery target ONLY when the founder EXPLICITLY bound it as one
  (a ``resource_bindings`` row), on top of it having a v1 event builder + an
  ``@p.outbound`` + a non-empty ``delivery_config``. Delivery is the founder's
  explicit choice — a connector is NOT swept in just because it carries a
  ``delivery_config`` (that config also configures NOTIFICATION channels, e.g. a
  telegram bot's ``{chat_id}``; without the explicit-binding gate the founder's
  telegram *notification* connector received a raw duplicate of every
  deliverable — implicit routing, which the product forbids).
* :func:`resolve_github_binding` — the github special case (NOT a simple event
  builder — it needs git-ops, not just an event dict). Used by the delivery
  adapter, the run-setup workspace provisioner that clones the github target,
  and the merge-watch poller — so it answers per the run's PRODUCT (#681), not
  per workspace: they must all agree on WHICH repo a run owns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.base import PluginMeta
from backend.identity.workspaces_db import ProductRow, ResourceBindingRow

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
    one ``@p.outbound``, a v1 event-builder exists for that connector, AND the
    founder EXPLICITLY bound the account as a delivery target — i.e. it has at
    least one :class:`ResourceBindingRow`. The explicit-binding gate is the
    guard against IMPLICIT ROUTING: a ``delivery_config`` alone does NOT make a
    connector a delivery target, because that same config configures a
    NOTIFICATION channel (e.g. a telegram bot's ``{chat_id, webhook_secret}``);
    without this gate the founder's telegram notification connector was swept in
    and got a raw duplicate of every deliverable. Rows failing any condition are
    skipped (a builder-less connector is the deliberate not-yet-wired seam; a
    binding-less one is simply not a delivery target the founder chose).
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
    # The connector_accounts the founder EXPLICITLY bound as delivery targets
    # (a resource_bindings row). Only these are swept into deliverable delivery.
    bound_account_ids: set[uuid.UUID] = set(
        (
            await session.execute(
                select(ResourceBindingRow.connector_account_id).where(
                    ResourceBindingRow.workspace_id == workspace_id
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
        if row.id not in bound_account_ids:
            # No explicit resource_binding → the founder never chose this
            # connector as a delivery target (it may be a notification-only
            # channel). Skipping it is what stops the implicit deliverable dump.
            logger.info(
                "connector_delivery_no_resource_binding_skipped",
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


def normalize_repo_slug(repo: str) -> str:
    """Normalize a repo URL or ``owner/name`` to a lowercase ``owner/name``.

    A product's ``repo_url`` is whatever the founder typed (a browser URL, one
    with a ``.git`` suffix, an ``ssh`` remote) while a connector's
    ``delivery_config['repo']`` is conventionally the bare ``owner/name`` —
    comparing the raw strings would call the SAME repo two different repos and
    silently fall back to "no binding matched". Both sides go through here so
    the comparison is about identity, not spelling.

    Deliberately duplicated from ``backend.api.webhooks._repo_slug`` (the
    inbound side, which binds a github issue to its product the same way): the
    R2c import-linter contract keeps the engine inbound layer out of this
    package, so it cannot import from here. The two are pinned against each
    other in ``tests/workflow/test_product_aware_github_binding.py`` so they
    cannot drift.
    """
    s = repo.strip().lower().removesuffix(".git")
    parts = [
        p
        for p in s.replace("https://", "")
        .replace("http://", "")
        .replace("git@", "")
        .replace(":", "/")
        .split("/")
        if p
    ]
    return "/".join(parts[-2:]) if len(parts) >= 2 else s


def display_repo_slug(repo: str) -> str:
    """``owner/name`` as the founder spelled it — the same parse, casing kept.

    :func:`normalize_repo_slug` lowercases for COMPARISON; this is what we hand
    to git as the remote, so ``blas1n/BStockReport`` must not come back
    ``blas1n/bstockreport``. (github resolves either, but the founder should
    recognise their own repo in a PR body and a log line.)
    """
    s = repo.strip()
    if s.lower().endswith(".git"):
        s = s[: -len(".git")]
    parts = [
        p
        for p in s.replace("https://", "")
        .replace("http://", "")
        .replace("git@", "")
        .replace(":", "/")
        .split("/")
        if p
    ]
    return "/".join(parts[-2:]) if len(parts) >= 2 else s


async def product_runs_in_place(session: AsyncSession, product_id: uuid.UUID | None) -> bool:
    """Does this product execute on the founder's own machine (``client_attach``)?

    Deliberately duplicated from
    ``backend.workflow.application.runtime.account_resolution.product_is_client_attach``
    — the same trade this package already makes for :func:`normalize_repo_slug`.
    The R2c import-linter contract keeps the engine inbound layer free of plugin
    imports, and that module reaches ``plugin.audit`` transitively, so importing
    it from here (even lazily — import-linter reads function-level imports too)
    puts ``backend.api.webhooks`` one hop from a plugin.

    Callers use it to withhold SERVER-SIDE SOURCE HANDLING: the clone at run
    setup, the re-clone behind the freshness merge. Never to withhold delivery
    itself — that conflation (#723) cost this execution model every PR it should
    have had.

    A missing product / unreadable metadata reads as the default model, matching
    the helper it mirrors.
    """
    if product_id is None:
        return False
    from backend.workflow.domain.execution_target import read_execution_target  # noqa: PLC0415

    metadata = await session.scalar(
        select(ProductRow.product_metadata).where(ProductRow.id == product_id)
    )
    return read_execution_target(metadata or {}) == "client_attach"


async def _product_repo(
    session: AsyncSession, product_id: uuid.UUID | None
) -> tuple[str, str] | None:
    """The repo the product OWNS as ``(display, normalized)``, or ``None``.

    ``None`` covers three cases that all mean "this call has no repo of its
    own, use the workspace target": no ``product_id`` was passed (a
    workspace-only caller), the product row is gone, or the product is
    substrate-only (blank ``repo_url``).
    """
    if product_id is None:
        return None
    repo_url = await session.scalar(select(ProductRow.repo_url).where(ProductRow.id == product_id))
    if not repo_url or not repo_url.strip():
        return None
    normalized = normalize_repo_slug(repo_url)
    if not normalized:
        return None
    return display_repo_slug(repo_url), normalized


async def resolve_github_binding(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    product_id: uuid.UUID | None = None,
) -> GithubBinding | None:
    """The github delivery target for ``product_id`` in ``workspace_id``, or ``None``.

    Mirrors :func:`_resolve_bindings` but for the github special case (github is
    NOT a simple event builder — it needs git-ops, not just an event dict). A
    row qualifies when it is ``is_active``, its ``connector`` is ``github``, and
    its ``delivery_config`` carries a non-empty ``repo``.

    **Product-scoped when the product owns a repo (#681).** This used to be
    purely workspace-scoped, so in a workspace holding two products EVERY run
    resolved the same first-row binding: a BStockReport run was provisioned as a
    ``BSVibe/bsvibe-app`` clone and would have pushed a branch + opened a PR
    there. So when the run's product carries a ``repo_url``, a binding whose
    ``repo`` names that same ``owner/name`` wins — and a binding naming a
    DIFFERENT repo is never used for it.

    **The connector is the CREDENTIAL; the product names the repo (#684).**
    A github App is installed account-wide, so pinning one repo per connector
    row forced a duplicate connector per product — and a fresh "Connect with
    GitHub" writes an EMPTY ``delivery_config``, which under #681 alone left
    every product in the workspace silently unbound (connected, credential
    valid, nothing delivered). So when the product owns a repo and no binding
    pins it, any active github connector serves as the credential and the
    PRODUCT's repo is the target. An explicit pin still wins when present —
    that is where a non-default ``base_branch`` lives.

    Only when there is no active github connector at all does a repo-owning
    product resolve ``None``. ``None`` is the deliberate safe outcome: the
    caller falls back to the product-workspace provisioner and skips github
    delivery, which beats writing to a repo the product does not own.

    **A binding is a credential and a repo — not a claim that a checkout exists
    here.** #723 made ``client_attach`` resolve ``None`` after a delivery crash
    (``git add -A`` against a directory the server never clones). That stopped
    the crash and took the whole delivery surface with it: no binding means no
    PR for any such run, ever. The thing that must never happen is the SERVER
    OBTAINING THE SOURCE, and the two callers that would — the run-setup
    provisioner and the merge-watch freshness merge — refuse it themselves now.
    Opening a PR needs no checkout at all, and since #735 the run has already
    pushed its branch from the founder's machine.

    A product with no ``repo_url`` (substrate-only) and a call with no
    ``product_id`` keep the previous workspace-scoped behaviour — they have no
    repo to infer, so only an explicitly pinned binding can answer them.

    Ordering is explicit (``created_at``, then ``id`` as the tie-break) rather
    than whatever the DB returns: with several qualifying accounts the old
    ``first()`` let the bootstrap path and the delivery path pick DIFFERENT
    accounts for the same workspace.
    """
    rows = (
        (
            await session.execute(
                select(ConnectorAccountRow)
                .where(
                    ConnectorAccountRow.workspace_id == workspace_id,
                    ConnectorAccountRow.connector == "github",
                    ConnectorAccountRow.is_active.is_(True),
                )
                .order_by(ConnectorAccountRow.created_at, ConnectorAccountRow.id)
            )
        )
        .scalars()
        .all()
    )
    owned = await _product_repo(session, product_id)
    # First active connector regardless of what it pins — the credential #684
    # falls back to. Ordering above makes "first" deterministic.
    credential: ConnectorAccountRow | None = None
    for row in rows:
        repo = (row.delivery_config or {}).get("repo")
        if owned is None:
            # No repo to infer: only an explicit pin can answer.
            if not repo:
                continue
            base_branch = str((row.delivery_config or {}).get("base_branch") or "main")
            return GithubBinding(account=row, repo=str(repo), base_branch=base_branch)
        if credential is None:
            credential = row
        if repo and normalize_repo_slug(str(repo)) == owned[1]:
            base_branch = str((row.delivery_config or {}).get("base_branch") or "main")
            return GithubBinding(account=row, repo=str(repo), base_branch=base_branch)
    if owned is not None and credential is not None:
        # #684 — nothing pins this repo, but the workspace HAS a github
        # credential. The product's own repo is the target; the connector just
        # authenticates. base_branch has no pin to read, so it is the default.
        return GithubBinding(account=credential, repo=owned[0], base_branch="main")
    if owned is not None:
        # Not an error: the workspace has no active github connector at all.
        # Logged because the founder's mental model ("my workspace has github")
        # says delivery should have happened — this line names the gap.
        logger.info(
            "github_binding_no_credential_for_product_repo",
            workspace_id=str(workspace_id),
            product_id=str(product_id),
            product_repo=owned[1],
            candidates=len(rows),
        )
    return None


__all__ = [
    "product_runs_in_place",
    "GithubBinding",
    "_Binding",
    "_resolve_bindings",
    "normalize_repo_slug",
    "resolve_github_binding",
]
