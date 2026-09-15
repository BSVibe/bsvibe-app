"""Tenant-table drift ratchet — a new workspace-scoped table cannot land unseen.

#959 measured the honest denominator: **38 tenant tables, 6 with RLS.** The
migration defends its narrow set (``20260612_gdpr_l1_and_rls.py:52-56`` — parent
FK CASCADE + the ORM auto-filter cover the children), and that defence holds for
the children it was written about. What it does NOT cover is the table that
arrives LATER: RLS DDL has been emitted exactly once in three months while ~10
workspace-scoped tables were added, and nothing outside that migration reads
``_RLS_TABLES``, so a new root tenant table enters with **no test going red.**

This test is that missing red. It pins three sets derived from the live mapper
registry:

* **tenant tables** — every mapped class carrying ``workspace_id`` (plus
  ``workspaces`` itself, keyed on ``id``);
* **auto-filter opt-outs** — classes declaring ``__exclude_workspace_filter__``;
* **RLS tables** — the migration's ``_RLS_TABLES``.

A new tenant table forces an explicit decision (add it here, and say whether it
needs RLS) instead of inheriting layer-2-only coverage by silence. Note what the
pin is: the SET OF TABLES, not a count and not a grep over spellings — a table
that renames itself is a new member and still fails.
"""

from __future__ import annotations

import importlib
import pkgutil
import uuid

from sqlalchemy import Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import backend
from backend.data import Base

# ---------------------------------------------------------------------------
# The pins. Adding a row here is the explicit decision the guard exists to force.
# ---------------------------------------------------------------------------

# Tables under Postgres RLS (defense layer 3). Kept in sync with
# ``_RLS_TABLES`` in the gdpr_l1_and_rls migration, which this test imports and
# compares against — so the pin cannot drift from the DDL that ran.
EXPECTED_RLS_TABLES: frozenset[str] = frozenset(
    {
        "workspaces",
        "products",
        "execution_runs",
        "deliverables",
        "execution_decisions",
        "requests",
    }
)

# Tenant tables NOT under RLS. Layer 2 (the ORM auto-filter) covers them; the
# migration's rationale is that each is a child of an RLS-covered root via an FK
# CASCADE. Adding a row here asserts that rationale still applies to it.
EXPECTED_RLS_EXEMPT_TENANT_TABLES: frozenset[str] = frozenset(
    {
        "account_embedding_settings",
        "accounts",
        "connector_accounts",
        "connector_oauth_pending",
        "delivery_events",
        "execution_run_activities",
        "execution_run_history",
        "executor_tasks",
        "executor_workers",
        "github_merge_watch",
        "intent_definitions",
        "intent_examples",
        "memberships",
        "model_accounts",
        "note_embeddings",
        "notification_events",
        "notification_prefs",
        "oauth_access_tokens",
        "oauth_clients",
        "oauth_codes",
        "oauth_device_codes",
        "ontology_corrections",
        "product_resources",
        "resource_bindings",
        "run_attempts",
        "run_routing_rules",
        "safe_mode_queue_items",
        "settle_drains",
        "trigger_events",
        "verification_results",
        "work_steps",
        "workspace_schedules",
    }
)

# Classes that opt OUT of the layer-2 auto-filter. Each needs a reason: the
# filter cannot scope the table the scope is resolved FROM, and the OAuth
# Authorization Server protocol tables are read before any workspace exists.
EXPECTED_AUTOFILTER_OPTOUTS: dict[str, str] = {
    "memberships": "resolution reads the workspace FROM this table",
    "oauth_access_tokens": "AS protocol table — read pre-workspace at token exchange",
    "oauth_clients": "AS protocol table — client lookup is workspace-less",
    "oauth_codes": "AS protocol table — authorization code exchange is pre-workspace",
    "oauth_device_codes": "AS protocol table — RFC 8628 device flow is pre-workspace",
    "oauth_refresh_tokens": "AS protocol table — refresh is pre-workspace",
}


def _import_every_backend_module() -> None:
    """Register every mapper — the registry only holds what has been imported.

    Without this the guard measures whatever the test session happened to load,
    which is exactly the kind of check that can only come out green.
    """
    for module in pkgutil.walk_packages(backend.__path__, "backend."):
        try:
            importlib.import_module(module.name)
        except Exception:  # noqa: BLE001, S112 — an unimportable module is not this test's subject
            continue


def _derive(registry: object | None = None) -> tuple[set[str], set[str]]:
    """(tenant tables, auto-filter opt-outs) from a mapper registry.

    ``registry=None`` means the live ``Base.registry`` — the production answer.
    The self-check below passes a throwaway registry so it can prove the
    derivation FLAGS a new tenant table without registering one on the real
    ``Base`` (which would leak into every other test in the session).
    """
    if registry is None:
        _import_every_backend_module()
        registry = Base.registry
    tenant: set[str] = set()
    optouts: set[str] = set()
    for mapper in registry.mappers:
        cls = mapper.class_
        table = mapper.local_table.name if mapper.local_table is not None else cls.__name__
        if getattr(cls, "__exclude_workspace_filter__", False):
            optouts.add(table)
        # ``workspaces`` is keyed on ``id``, every other tenant table on ``workspace_id``.
        if "workspace_id" in mapper.columns or table == "workspaces":
            tenant.add(table)
    return tenant, optouts


def _migration_rls_tables() -> frozenset[str]:
    """``_RLS_TABLES`` read out of the migration module itself.

    Loaded by path — the module name starts with a digit, so it is not
    importable. Reading the DDL's own tuple (rather than restating it) is what
    keeps the pin from drifting away from what actually ran.
    """
    import importlib.util  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    path = (
        Path(backend.__file__).parent
        / "data"
        / "migrations"
        / "versions"
        / "20260612_gdpr_l1_and_rls.py"
    )
    spec = importlib.util.spec_from_file_location("_gdpr_l1_and_rls", path)
    assert spec is not None and spec.loader is not None, f"migration not found at {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return frozenset(module._RLS_TABLES)  # noqa: SLF001 — reading the DDL's own list is the point


def test_every_tenant_table_is_pinned() -> None:
    """A new workspace-scoped table must be classified, not inherited by silence."""
    tenant, _ = _derive()
    pinned = EXPECTED_RLS_TABLES | EXPECTED_RLS_EXEMPT_TENANT_TABLES
    unpinned = sorted(tenant - pinned)
    assert not unpinned, (
        "new workspace-scoped table(s) with no isolation decision recorded. "
        "Decide: does it need Postgres RLS (layer 3), or is the ORM auto-filter "
        "(layer 2) plus an FK CASCADE to an RLS-covered parent enough? Then add "
        "it to EXPECTED_RLS_TABLES or EXPECTED_RLS_EXEMPT_TENANT_TABLES:\n  "
        + "\n  ".join(unpinned)
    )
    # The other direction is also the control on the derivation itself: if the
    # import sweep above silently failed, ``tenant`` comes back short and this
    # assertion — not a green pass — is what happens.
    stale = sorted(pinned - tenant)
    assert not stale, (
        "pinned table(s) no longer carry workspace_id (renamed or dropped), or "
        "the mapper sweep did not import them — remove them so the pin cannot "
        "rot into a guard that guards nothing:\n  " + "\n  ".join(stale)
    )


def test_rls_pin_matches_the_migration() -> None:
    """The pin is the DDL's own list — not a second, drifting copy of it."""
    assert _migration_rls_tables() == EXPECTED_RLS_TABLES


def test_autofilter_optouts_are_pinned_with_a_reason() -> None:
    """Opting a table out of layer 2 is the one change that silently removes a
    guard, so it may never happen without a line in this file."""
    _, optouts = _derive()
    added = sorted(optouts - set(EXPECTED_AUTOFILTER_OPTOUTS))
    assert not added, (
        "table(s) opted OUT of the ORM workspace auto-filter without a recorded "
        "reason — this removes layer 2 for them:\n  " + "\n  ".join(added)
    )
    removed = sorted(set(EXPECTED_AUTOFILTER_OPTOUTS) - optouts)
    assert not removed, (
        "pinned opt-out(s) no longer opt out — drop them from the pin:\n  " + "\n  ".join(removed)
    )


# ---------------------------------------------------------------------------
# Self-check: the derivation actually FLAGS a new tenant table (RED proof).
# Without this the three tests above could be green because they see nothing.
# ---------------------------------------------------------------------------
def test_derivation_flags_a_synthetic_new_tenant_table() -> None:
    class _ThrowawayBase(DeclarativeBase):
        pass

    class _NewTenantTable(_ThrowawayBase):
        __tablename__ = "synthetic_new_tenant_table"
        id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
        workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)

    class _OptedOut(_ThrowawayBase):
        __tablename__ = "synthetic_optout_table"
        __exclude_workspace_filter__ = True
        id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
        workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)

    tenant, optouts = _derive(_ThrowawayBase.registry)
    assert "synthetic_new_tenant_table" in tenant
    assert "synthetic_optout_table" in optouts
    assert "synthetic_new_tenant_table" not in (
        EXPECTED_RLS_TABLES | EXPECTED_RLS_EXEMPT_TENANT_TABLES
    )
