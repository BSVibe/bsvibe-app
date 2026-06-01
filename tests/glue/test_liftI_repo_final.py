"""Lift I-Repo-Final — close out the Repository pass.

Three phases:

* Phase A — ``backend/workspaces/`` absorbed into ``backend/identity/``. The
  legacy directory must be gone; ``WorkspaceRow`` / ``ProductRow`` /
  ``ProductResourceRow`` / ``ResourceBindingRow`` live in
  :mod:`backend.identity.workspaces_db`; ``ResourceBindingRepository`` has
  a Protocol + SQL impl under
  :mod:`backend.identity.{domain,infrastructure}.repositories`.

* Phase B — Schedule context Repository: ``WorkspaceScheduleRepository``
  Protocol + concrete in
  :mod:`backend.schedule.{domain,infrastructure}.repositories`.

* Phase C — Extensions context: documented as a no-violations finding
  (no Repository needed). This file's Phase-C deltas assert ZERO direct
  ``session.execute`` / ``session.add`` / ``sqlalchemy`` imports under
  :mod:`backend.extensions` — keeping Phase C honest going forward.

The deltas below are RED-first proof.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Phase A — workspaces absorption into Identity
# ---------------------------------------------------------------------------


def test_phaseA_legacy_workspaces_package_is_gone() -> None:
    """``backend/workspaces/`` directory is removed by the absorption."""
    repo_root = Path(__file__).resolve().parents[2]
    legacy_dir = repo_root / "backend" / "workspaces"
    assert not legacy_dir.exists(), (
        "backend/workspaces/ must be absorbed into backend/identity/ — directory still present."
    )


@pytest.mark.parametrize(
    "name",
    [
        "WorkspaceRow",
        "ProductRow",
        "ProductResourceRow",
        "ResourceBindingRow",
        "WorkspacesBase",
        "validate_legal_basis",
    ],
)
def test_phaseA_rows_at_new_identity_location(name: str) -> None:
    """The four row classes + helpers are importable from identity.workspaces_db."""
    mod = importlib.import_module("backend.identity.workspaces_db")
    assert hasattr(mod, name), f"{name} missing from backend.identity.workspaces_db"


def test_phaseA_resource_binding_repository_protocol() -> None:
    """Protocol lives in identity.domain.repositories.resource_binding_repository."""
    mod = importlib.import_module(
        "backend.identity.domain.repositories.resource_binding_repository"
    )
    assert hasattr(mod, "ResourceBindingRepository")


def test_phaseA_resource_binding_repository_sql_at_new_location() -> None:
    """Concrete impl lives in identity.infrastructure.repositories."""
    mod = importlib.import_module(
        "backend.identity.infrastructure.repositories.resource_binding_repository_sql"
    )
    assert hasattr(mod, "SqlAlchemyResourceBindingRepository")


def test_phaseA_no_legacy_workspaces_imports_in_app_code() -> None:
    """No code under ``backend/`` (except identity package shim) imports backend.workspaces."""
    repo_root = Path(__file__).resolve().parents[2]
    backend_dir = repo_root / "backend"
    offenders: list[str] = []
    for path in backend_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "from backend.workspaces" in text or "import backend.workspaces" in text:
            offenders.append(str(path.relative_to(repo_root)))
    assert not offenders, f"Legacy backend.workspaces imports survive: {offenders}"


# ---------------------------------------------------------------------------
# Phase B — Schedule Repository
# ---------------------------------------------------------------------------


def test_phaseB_workspace_schedule_repository_protocol() -> None:
    mod = importlib.import_module(
        "backend.schedule.domain.repositories.workspace_schedule_repository"
    )
    assert hasattr(mod, "WorkspaceScheduleRepository")


def test_phaseB_workspace_schedule_repository_sql() -> None:
    mod = importlib.import_module(
        "backend.schedule.infrastructure.repositories.workspace_schedule_repository_sql"
    )
    assert hasattr(mod, "SqlAlchemyWorkspaceScheduleRepository")


def test_phaseB_db_poll_runner_uses_repository() -> None:
    """``DbPollScheduleRunner`` no longer issues raw ``session.execute`` for schedule rows."""
    mod = importlib.import_module("backend.schedule.infrastructure.db_poll_runner")
    src = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    assert "WorkspaceScheduleRepository" in src or "workspace_schedule_repository" in src, (
        "DbPollScheduleRunner must depend on WorkspaceScheduleRepository seam"
    )
    # The runner no longer issues a raw select() against WorkspaceScheduleRow.
    assert "select(WorkspaceScheduleRow)" not in src


# ---------------------------------------------------------------------------
# Phase C — Extensions: documented as no-violations finding
# ---------------------------------------------------------------------------


def test_phaseC_extensions_has_no_app_layer_db_violations() -> None:
    """``backend/extensions/`` carries ZERO direct sqlalchemy use.

    Phase C of Lift I-Repo-Final concluded a Repository is not required for
    the Extensions context — none of its modules touch the DB at the
    application layer. Lock that in as a guard test so a future change
    that reintroduces a violation fails fast.
    """
    repo_root = Path(__file__).resolve().parents[2]
    ext_dir = repo_root / "backend" / "extensions"
    offenders: list[str] = []
    for path in ext_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if (
            "session.execute" in text
            or "session.add" in text
            or "from sqlalchemy" in text
            or "import sqlalchemy" in text
        ):
            offenders.append(str(path.relative_to(repo_root)))
    assert not offenders, f"Extensions gained an app-layer DB violation: {offenders}"
