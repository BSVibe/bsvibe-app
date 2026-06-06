"""Slice 0.2 — OAuth token / pending-state persistence models.

Structural tests (no live DB): the two tables the OAuth dance needs exist,
register on ``Base.metadata`` (so alembic env.py + autogenerate see them),
and carry the columns the storage + callback layers rely on:

* ``connector_oauth_tokens`` — encrypted access/refresh material, 1:1 with a
  ``connector_accounts`` row (FK + unique), nullable refresh/expiry (some
  providers issue non-expiring tokens).
* ``connector_oauth_pending`` — short-lived CSRF ``state`` + PKCE verifier
  held between ``/start`` and ``/callback``. No FK to connector_accounts: the
  account may not exist until the callback creates it.

The live fresh-PG round-trip is covered by tests/test_alembic_fresh.py.
"""

from __future__ import annotations

from backend.connectors.auth.db import (
    ConnectorOAuthPendingRow,
    ConnectorOAuthTokenRow,
)
from backend.data import Base


def _columns(model: type) -> dict[str, object]:
    return {c.name: c for c in model.__table__.columns}  # type: ignore[attr-defined]


# ── connector_oauth_tokens ────────────────────────────────────────────


def test_token_table_name() -> None:
    assert ConnectorOAuthTokenRow.__tablename__ == "connector_oauth_tokens"


def test_token_registered_on_metadata() -> None:
    assert "connector_oauth_tokens" in Base.metadata.tables


def test_token_has_expected_columns() -> None:
    cols = _columns(ConnectorOAuthTokenRow)
    for name in (
        "id",
        "connector_account_id",
        "provider",
        "access_token_ciphertext",
        "refresh_token_ciphertext",
        "expires_at",
        "scopes",
        "account_label",
        "created_at",
        "updated_at",
    ):
        assert name in cols, f"missing column {name}"


def test_token_account_fk_and_unique() -> None:
    col = _columns(ConnectorOAuthTokenRow)["connector_account_id"]
    # FK to connector_accounts.
    fks = list(col.foreign_keys)
    assert fks, "connector_account_id must be a foreign key"
    assert fks[0].column.table.name == "connector_accounts"
    # 1:1 — unique on the account id.
    assert col.unique is True


def test_token_refresh_and_expiry_nullable() -> None:
    cols = _columns(ConnectorOAuthTokenRow)
    assert cols["refresh_token_ciphertext"].nullable is True
    assert cols["expires_at"].nullable is True
    # access token is mandatory.
    assert cols["access_token_ciphertext"].nullable is False


# ── connector_oauth_pending ───────────────────────────────────────────


def test_pending_table_name() -> None:
    assert ConnectorOAuthPendingRow.__tablename__ == "connector_oauth_pending"


def test_pending_registered_on_metadata() -> None:
    assert "connector_oauth_pending" in Base.metadata.tables


def test_pending_has_expected_columns() -> None:
    cols = _columns(ConnectorOAuthPendingRow)
    for name in (
        "state",
        "provider",
        "workspace_id",
        "code_verifier",
        "redirect_uri",
        "created_at",
    ):
        assert name in cols, f"missing column {name}"


def test_pending_state_is_primary_key() -> None:
    col = _columns(ConnectorOAuthPendingRow)["state"]
    assert col.primary_key is True


def test_pending_has_no_account_fk() -> None:
    # The account may not exist until the callback creates it — pending must
    # not depend on a connector_accounts row.
    for col in ConnectorOAuthPendingRow.__table__.columns:  # type: ignore[attr-defined]
        assert not col.foreign_keys, f"unexpected FK on {col.name}"
