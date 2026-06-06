"""Workspace + Product + ResourceBinding persistence schema (Workflow §3).

Lift I-Repo-Final Phase A: absorbed from the deleted ``backend.workspaces``
common-leaf package into the :mod:`backend.identity` bounded context. The
Workspace + Product + ProductResource + ResourceBinding rows are
identity-domain entities (the workspace is the multi-tenancy unit; products
are per-workspace shipping units; bindings are the founder-owned 3-knob
identity for each connector resource a product cares about). Keeping them
under :mod:`backend.identity` puts the SQLAlchemy schema next to the
Repository Protocols + concrete impls (Lift I-Repo-Identity).

The legacy module path ``backend.workspaces.db`` is REMOVED — callers must
import from :mod:`backend.identity.workspaces_db`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, get_args

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

# Kept as an alias for back-compat with old call-sites that imported
# ``WorkspacesBase`` from the legacy ``backend.workspaces.db`` path. Points at
# the same :class:`backend.data.Base` the rest of the schema uses.
WorkspacesBase = Base

# GDPR L1 — Art. 6 legal-basis marker. v1 carries only the two bases that
# describe BSVibe's own model: ``contract`` (the workspace founder operating
# under our service contract) and ``consent`` (an end-user-driven workspace
# operating on opt-in consent). TEXT + ``Literal`` validation keeps this
# portable across the SQLite test tier and Postgres without enum DDL
# gymnastics in the migration (same shape as ``resource_bindings.output_mode``).
LegalBasis = Literal["contract", "consent"]
_LEGAL_BASIS_VALUES: frozenset[str] = frozenset(get_args(LegalBasis))


def validate_legal_basis(value: str) -> LegalBasis:
    """Raise ``ValueError`` unless ``value`` is a recognised legal basis."""
    if value not in _LEGAL_BASIS_VALUES:
        raise ValueError(
            f"invalid legal_basis {value!r}; must be one of {sorted(_LEGAL_BASIS_VALUES)}"
        )
    return value  # type: ignore[return-value]


class WorkspaceRow(WorkspacesBase):
    """Top-level multi-tenancy unit."""

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Region per Workflow §2.3 — vault/<region>/<workspace_id>/ FS layout
    region: Mapped[str] = mapped_column(String(32), nullable=False, default="us-1")
    safe_mode: Mapped[bool] = mapped_column(nullable=False, default=True)
    # GDPR L1 — Art. 6 legal basis the workspace operates under. TEXT + app
    # Literal validation (see :func:`validate_legal_basis`). Default mirrors
    # BSVibe's v1 product reality: every workspace is a founder operating
    # under our service contract until a future deployment opens consent-based
    # workspaces.
    legal_basis: Mapped[str] = mapped_column(
        String(32), nullable=False, default="contract", server_default="contract"
    )
    # Lift Q1 — per-workspace audit_outbox retention knob (roadmap §6 결정 로그 Q1).
    # ``NULL`` = forever (the architectural default, what most workspaces
    # leave untouched). An integer ``N >= 1`` = the daily retention sweep
    # (:class:`plugin.audit.retention_sweep.AuditRetentionSweepRunner`)
    # deletes ``audit_outbox`` rows for this workspace whose
    # ``occurred_at < now - N * 1d``. The knob being THERE (vs. a system
    # constant) is the architectural deliverable; most workspaces never set
    # it. Validation (``N >= 1``) lives at the REST surface — the column is
    # an open INTEGER so a future settings-row migration doesn't need a
    # schema change.
    audit_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # Lift E1 — workspace-default ModelAccount fallback for the new
    # :class:`backend.dispatch.resolver.ModelAccountResolver`. The founder
    # picks this through Settings → Models or the MCP tool
    # ``bsvibe_workspace_set_default_account`` once a workspace has at
    # least one active :class:`ModelAccount`. ``NULL`` (the default) is
    # the architectural baseline — BSVibe NEVER auto-stamps it (founder
    # policy ``bsvibe-no-implicit-routing``: routing is the user's
    # decision). When the resolver finds no matching rule and the
    # column is ``NULL`` it raises
    # :class:`~backend.dispatch.resolver.NoMatchingRouteError` rather
    # than silently picking a model. The FK is ``ON DELETE SET NULL`` so
    # deleting the model account leaves the workspace row intact (the
    # founder will be re-prompted to pick a default).
    default_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(),
        onupdate=lambda: datetime.now(),
    )
    # Soft delete (Workflow §10.7): set on delete; the 30-day-window hard
    # purge + full cascade is a retention-infra follow-up.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProductRow(WorkspacesBase):
    """Per-workspace shipping unit."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_products_ws_slug"),
        # Lift A v2 — founder UI hits this on every Product detail page that
        # carries an in-flight bootstrap. Composite so the workspace scope is
        # already pruned by the same index seek.
        Index("ix_products_ws_bootstrap_status", "workspace_id", "bootstrap_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    repo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Lift A v2 — repo-URL bootstrap telemetry. All four columns are NULL on a
    # product created without a ``repo_url`` (the bootstrap job is skipped).
    # ``bootstrap_status`` walks the lifecycle vocabulary documented on the
    # migration (``pending`` → ``cloning`` → ``analyzing`` → ``ingesting`` →
    # ``complete`` / ``failed:<reason>``). ``bootstrap_run_id`` is a loose
    # correlation id for log lookup (not a FK — the job is in-process today).
    bootstrap_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    bootstrap_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    bootstrap_artifacts_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bootstrap_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(),
        onupdate=lambda: datetime.now(),
    )


class ProductResourceRow(WorkspacesBase):
    """A named pointer a product works with — a repo, doc, deploy, or note.

    Workspace-scoped (carries ``workspace_id`` so the global ORM auto-filter
    engages) and parented to a ``Product`` via ``product_id`` with an
    ``ON DELETE CASCADE`` FK, so a product's resources go with it. ``kind`` is
    a short free-string tag (``link`` / ``doc`` / ``repo`` / ``note`` …) the
    UI renders as a chip; ``url`` and ``note`` are both optional.
    """

    __tablename__ = "product_resources"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    note: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class ResourceBindingRow(WorkspacesBase):
    """Per-Product × Connector 3-knob binding (Workflow §3).

    A *Resource* in the spec sense — the binding that carries the founder-set
    knobs for one Product × ConnectorAccount pairing:

    * ``selection`` — connector-shaped scope (e.g. ``{"labels": ["bug"]}``).
    * ``trigger`` — ``{"enabled": bool, "filters": dict}`` (the *do I act* knob).
    * ``output_mode`` — ``'safe'`` (queue for founder approval, default) or
      ``'direct'`` (deliver straight out). See Workflow §1/§3/§12.5.

    Workspace-scoped (``workspace_id``) so the global ORM auto-filter engages;
    parented to ``products`` and ``connector_accounts`` via FKs with
    ``ON DELETE CASCADE`` — a product or account removal cascades to its
    bindings. ``resource_id`` is the *connector-side* identifier (e.g. a GitHub
    ``"bsvibe/bsvibe-site"``); the ``(connector_account_id, resource_id)`` index
    is what Receive (B10b) will use to resolve an inbound webhook → binding →
    Product.
    """

    __tablename__ = "resource_bindings"
    __table_args__ = (
        Index(
            "ix_resource_bindings_product_id",
            "product_id",
        ),
        Index(
            "ix_resource_bindings_lookup",
            "connector_account_id",
            "resource_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connector_accounts.id", ondelete="CASCADE"), nullable=False
    )
    # Connector-shaped opaque identifier ("bsvibe/bsvibe-site#42",
    # "C123/THREAD", …). Free string — each connector defines its own grammar.
    resource_id: Mapped[str] = mapped_column(String(512), nullable=False)
    # Selection scope (connector-shaped). Empty ``{}`` = the whole resource.
    selection: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # Trigger knob: {"enabled": bool, "filters": dict}. Default = disabled,
    # no filters (the safest default — a fresh binding doesn't auto-fire).
    trigger: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=lambda: {"enabled": False, "filters": {}}
    )
    # Output mode: 'safe' = Safe Mode queue (founder approves), 'direct' =
    # auto-deliver. TEXT + app-side validation keeps this portable across the
    # SQLite test tier and Postgres (no enum DDL gymnastics in migrations).
    output_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="safe")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(),
        onupdate=lambda: datetime.now(),
    )
