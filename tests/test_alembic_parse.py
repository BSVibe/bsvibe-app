"""Smoke: alembic history loads, head revision is registered.

Doesn't apply the migration (no live PG required) — just verifies the
revision file parses + the env.py target_metadata aggregates every
Bundle 1 base. A fresh-PG integration test (live ``alembic upgrade
head`` against a throwaway PG) lands in CI alongside the workers
bundle, when the deploy stack already starts PG service containers
for end-to-end smoke.
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
    assert "bundle1_initial" in result.stdout


def test_target_metadata_covers_all_bases():
    """Reach into env.py module to confirm the merged metadata sees
    every base we expect."""
    from backend.accounts.models import AccountsBase
    from backend.gateway.budget.models import GatewayBudgetBase
    from backend.supervisor.audit.models import AuditOutboxBase, SupervisorBase

    expected_tables = {
        "model_accounts",
        "account_budget_policies",
        "audit_events",
        "audit_outbox",
    }
    actual_tables = (
        set(AccountsBase.metadata.tables)
        | set(GatewayBudgetBase.metadata.tables)
        | set(SupervisorBase.metadata.tables)
        | set(AuditOutboxBase.metadata.tables)
    )
    assert expected_tables.issubset(actual_tables), (
        f"Missing tables: {expected_tables - actual_tables}"
    )
