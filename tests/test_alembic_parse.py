"""Smoke: alembic history loads, every base is reachable from env.py.

Doesn't apply the migration (no live PG required) — just verifies the
revision files parse + the env.py target_metadata aggregates every
declarative base in scope. The fresh-PG round-trip lives in
:mod:`tests.test_alembic_fresh` (gated on ``BSVIBE_DATABASE_URL``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_alembic_history_loads():
    repo = Path(__file__).parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "history"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic history failed: {result.stderr}"
    # Every shipped revision must appear in the chain.
    for rev in (
        "bundle1_initial",
        "bundle1_5a_rules",
        "bundle1_5b_routing_embed",
        "bundle_k_knowledge",
        "bundle_x_execution",
        "bundle_g_glue",
        "bundle_h_workspaces",
        "phase1_auth_identity",
        "settle_drains",
        "connector_accounts",
        "decision_resolve",
        "connector_delivery_config",
        "accounts",
        "notification_prefs",
    ):
        assert rev in result.stdout, f"missing revision {rev} in:\n{result.stdout}"


def test_alembic_head_is_notification_prefs():
    repo = Path(__file__).parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "notification_prefs" in result.stdout


def test_target_metadata_covers_all_bases():
    """Reach into env.py module to confirm the merged metadata sees
    every base we expect — Bundle 1 + Bundle 1.5a/1.5b + Bundle K + Bundle X."""
    import backend.accounts.account_models  # noqa: F401 — registers `accounts` table
    from backend.accounts.models import AccountsBase
    from backend.connectors.db import ConnectorsBase
    from backend.delivery.db import DeliveryBase
    from backend.execution.db import ExecutionBase
    from backend.gateway.budget.models import GatewayBudgetBase
    from backend.gateway.embedding.db import GatewayEmbeddingBase
    from backend.gateway.routing.db import GatewayRoutingBase
    from backend.gateway.rules.db import GatewayRulesBase
    from backend.identity.db import IdentityBase
    from backend.intake.db import IntakeBase
    from backend.knowledge.canonicalization.db import CanonicalizationBase
    from backend.knowledge.ingest.db import IngestBase
    from backend.knowledge.retrieval.db import RetrievalBase
    from backend.notifications.db import NotificationsBase
    from backend.supervisor.audit.models import AuditOutboxBase, SupervisorBase
    from backend.workers.db import WorkersBase
    from backend.workspaces.db import WorkspacesBase

    expected_tables = {
        # Bundle 1
        "model_accounts",
        "account_budget_policies",
        "audit_events",
        "audit_outbox",
        # Bundle 1.5a
        "routing_rules",
        "rule_conditions",
        # Bundle 1.5b
        "account_embedding_settings",
        "intent_definitions",
        "intent_examples",
        "model_catalog_entries",
        "routing_logs",
        # Bundle K
        "canonical_anchors",
        "canonicalization_proposals",
        "canonicalization_decisions",
        "canonicalization_policies",
        "ingest_batches",
        "retrieval_queries",
        # Bundle X
        "execution_runs",
        "execution_run_history",
        "execution_run_activities",
        "composition_snapshots",
        "decomposer_steps",
        "work_steps",
        "run_attempts",
        "deliverables",
        "execution_decisions",
        "verification_results",
        # Bundle G
        "trigger_events",
        "requests",
        "delivery_events",
        "safe_mode_queue_items",
        "workers",
        "worker_install_tokens",
        "audit_relay_state",
        # Bundle H
        "workspaces",
        "products",
        # Phase 1 auth — identity
        "users",
        "memberships",
        # Settle drain marker (worker-settle BSage write subscriber)
        "settle_drains",
        # Connector-inbound webhook bindings (Workflow §11.2)
        "connector_accounts",
        # Per-workspace personal billing account (the account axis)
        "accounts",
        # Per-workspace notification preferences (events x channels + quiet hours)
        "notification_prefs",
    }
    actual_tables = (
        set(AccountsBase.metadata.tables)
        | set(GatewayBudgetBase.metadata.tables)
        | set(GatewayRulesBase.metadata.tables)
        | set(GatewayEmbeddingBase.metadata.tables)
        | set(GatewayRoutingBase.metadata.tables)
        | set(SupervisorBase.metadata.tables)
        | set(AuditOutboxBase.metadata.tables)
        | set(CanonicalizationBase.metadata.tables)
        | set(IngestBase.metadata.tables)
        | set(RetrievalBase.metadata.tables)
        | set(ExecutionBase.metadata.tables)
        | set(IntakeBase.metadata.tables)
        | set(DeliveryBase.metadata.tables)
        | set(WorkersBase.metadata.tables)
        | set(WorkspacesBase.metadata.tables)
        | set(IdentityBase.metadata.tables)
        | set(ConnectorsBase.metadata.tables)
        | set(NotificationsBase.metadata.tables)
    )
    assert expected_tables.issubset(actual_tables), (
        f"Missing tables: {expected_tables - actual_tables}"
    )
