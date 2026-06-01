"""GDPR L1 — PII catalog must reference real columns on real tables.

The catalog (:mod:`backend.data.pii`) is a frozen metadata declaration of
which columns hold personal data — it carries no behavior, but exists so a
future erasure / export refinement has a single referenceable source. If
the model layer drifts (column rename, table rename) the catalog must fail
loudly at import time, not silently rot.
"""

from __future__ import annotations

import backend.identity.db  # noqa: F401 — registers users + memberships
import backend.identity.workspaces_db  # noqa: F401 — registers workspaces + products
import backend.notifications.db  # noqa: F401 — registers notification_prefs

# Importing the relevant db modules for table registration side effects.
import backend.workflow.infrastructure.db  # noqa: F401 — registers execution_decisions
import backend.workflow.infrastructure.intake.db  # noqa: F401 — registers requests + trigger_events
from backend.data import Base
from backend.data.pii import PII_CATALOG


def test_pii_catalog_columns_exist_on_models() -> None:
    """Every (table, column) declared in the catalog resolves to a live mapping."""
    tables = Base.metadata.tables
    for table_name, columns in PII_CATALOG.items():
        assert table_name in tables, f"PII catalog references unknown table {table_name!r}"
        table = tables[table_name]
        for column_name in columns:
            assert column_name in table.c, (
                f"PII catalog references unknown column "
                f"{table_name}.{column_name} (live columns: {list(table.c.keys())})"
            )


def test_pii_catalog_is_immutable() -> None:
    """The exported catalog object must be frozen so callers cannot mutate it."""
    # A Mapping that doesn't expose __setitem__ at type level — assert at
    # runtime that mutating raises.
    import pytest

    with pytest.raises(TypeError):
        PII_CATALOG["new_table"] = ("col",)  # type: ignore[index]


def test_pii_catalog_covers_known_pii_surfaces() -> None:
    """The catalog must declare the known sensitive surfaces."""
    assert "users" in PII_CATALOG
    assert "email" in PII_CATALOG["users"]
    assert "memberships" in PII_CATALOG
