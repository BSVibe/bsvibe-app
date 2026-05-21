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
    ):
        assert rev in result.stdout, f"missing revision {rev} in:\n{result.stdout}"


def test_alembic_head_is_routing_embed():
    repo = Path(__file__).parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "bundle1_5b_routing_embed" in result.stdout


def test_target_metadata_covers_all_bases():
    """Reach into env.py module to confirm the merged metadata sees
    every base we expect — Bundle 1 + Bundle 1.5a/1.5b."""
    from backend.accounts.models import AccountsBase
    from backend.gateway.budget.models import GatewayBudgetBase
    from backend.gateway.embedding.db import GatewayEmbeddingBase
    from backend.gateway.routing.db import GatewayRoutingBase
    from backend.gateway.rules.db import GatewayRulesBase
    from backend.supervisor.audit.models import AuditOutboxBase, SupervisorBase

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
    }
    actual_tables = (
        set(AccountsBase.metadata.tables)
        | set(GatewayBudgetBase.metadata.tables)
        | set(GatewayRulesBase.metadata.tables)
        | set(GatewayEmbeddingBase.metadata.tables)
        | set(GatewayRoutingBase.metadata.tables)
        | set(SupervisorBase.metadata.tables)
        | set(AuditOutboxBase.metadata.tables)
    )
    assert expected_tables.issubset(actual_tables), (
        f"Missing tables: {expected_tables - actual_tables}"
    )
