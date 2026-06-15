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
        "executor_workers",
        "executor_tasks",
        "model_account_nullable_key",
        "product_resources",
        "executor_artifact_capture",
        "resource_bindings",
        "mid_loop_deliver",
        "compensation_wiring",
        "gdpr_l1_and_rls",
        "product_id_not_null",
        "backfill_ship_or_discard",
        "w1_workspace_cleanup",
        "run_routing_rules",
        "workspace_schedules",
        "workspace_audit_retention",
        "ontology_corrections",
        "connector_last_import",
        "product_bootstrap",
        "oauth_authorization_server",
        "oauth_anonymous_dcr",
        "connector_oauth_tokens",
        "connector_oauth_app_credentials",
        "connector_oauth_unclaimed",
        "workspace_default_account",
    ):
        assert rev in result.stdout, f"missing revision {rev} in:\n{result.stdout}"


def test_alembic_head_is_connector_last_import():
    repo = Path(__file__).parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    # Lift E16 → worker_last_in_flight; E21 → executor_task_model; E32 →
    # executor_task_repo_url. Keep the test name (function name is the
    # historical revision id, kept for git-blame stability) and assert
    # the current tip.
    assert "executor_task_repo_url" in result.stdout


def test_target_metadata_covers_all_bases():
    """Reach into env.py module to confirm the merged metadata sees
    every base we expect — Bundle 1 + Bundle 1.5a/1.5b + Bundle K + Bundle X."""
    import backend.router.accounts.account_models  # noqa: F401 — registers `accounts` table
    from backend.connectors.db import ConnectorsBase
    from backend.embedding.db import GatewayEmbeddingBase
    from backend.executors.db import ExecutorsBase
    from backend.identity.db import IdentityBase
    from backend.identity.workspaces_db import WorkspacesBase
    from backend.knowledge.canonicalization.db import CanonicalizationBase
    from backend.knowledge.ingest.db import IngestBase
    from backend.knowledge.retrieval.db import RetrievalBase
    from backend.notifications.db import NotificationsBase
    from backend.router.accounts.models import AccountsBase
    from backend.router.budget.models import GatewayBudgetBase
    from backend.router.routing.db import GatewayRoutingBase
    from backend.router.rules.db import GatewayRulesBase
    from backend.workers.db import WorkersBase
    from backend.workflow.infrastructure.db import ExecutionBase
    from backend.workflow.infrastructure.delivery.db import DeliveryBase
    from backend.workflow.infrastructure.intake.db import IntakeBase
    from plugin.audit.models import AuditOutboxBase, SupervisorBase

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
        # M1 — schedule runner emitter source
        "workspace_schedules",
        # Bundle H
        "workspaces",
        "products",
        # Per-product resources (repo / doc / deploy / note pointers)
        "product_resources",
        # Per-Product × Connector 3-knob binding (Workflow §3 — selection +
        # trigger + output_mode).
        "resource_bindings",
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
        # External executor-worker registration subsystem (executor-pool Lift 1)
        "executor_workers",
        # Executor dispatch substrate — pending→dispatched→done/failed (Lift 2)
        "executor_tasks",
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
        | set(ExecutorsBase.metadata.tables)
    )
    assert expected_tables.issubset(actual_tables), (
        f"Missing tables: {expected_tables - actual_tables}"
    )
