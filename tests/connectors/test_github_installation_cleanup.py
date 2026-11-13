"""설치 참조 1회 정리 마이그레이션 — claim 기록과 맞지 않는 숫자 external_ref 만 비운다."""

from __future__ import annotations

import importlib.util
import logging
import uuid
from pathlib import Path

import sqlalchemy as sa

import backend.connectors.auth.db  # noqa: F401 — 토큰 테이블 등록
from backend.connectors.db import ConnectorAccountRow
from backend.data import Base

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "backend/data/migrations/versions/20261113_github_installation_ref_cleanup.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("gh_ref_cleanup", _MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _add(conn: sa.Connection, connector: str, ref: str | None, *, claimed: bool = False) -> str:
    aid = uuid.uuid4()
    conn.execute(
        sa.insert(ConnectorAccountRow.__table__).values(
            id=aid,
            workspace_id=uuid.uuid4(),
            connector=connector,
            webhook_token="wht_" + aid.hex,
            signing_secret_ciphertext="x",
            external_ref=ref,
            delivery_config={},
            is_active=True,
        )
    )
    if claimed:
        conn.execute(
            sa.text(
                "INSERT INTO connector_oauth_tokens "
                "(id, connector_account_id, provider, access_token_ciphertext, scopes, status, "
                "created_at, updated_at) VALUES "
                "(:i, :a, 'github', 'c', '[]', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ).bindparams(i=uuid.uuid4().hex, a=aid.hex)
        )
    return aid.hex


def _ref(conn: sa.Connection, aid: str) -> str | None:
    return conn.execute(
        sa.text("SELECT external_ref FROM connector_accounts WHERE id = :i").bindparams(i=aid)
    ).scalar_one()


def test_unmatched_numeric_ref_is_cleared_matched_kept(caplog) -> None:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        forged = _add(conn, "github", "999")
        claimed = _add(conn, "github", "123", claimed=True)
        non_numeric = _add(conn, "github", "o/r")
        other = _add(conn, "sentry", "555")
        with caplog.at_level(logging.WARNING):
            cleared = _load().clear_unverified_installation_refs(conn)
        assert cleared == [forged]
        assert _ref(conn, forged) is None
        assert _ref(conn, claimed) == "123"
        assert _ref(conn, non_numeric) == "o/r"
        assert _ref(conn, other) == "555"
    assert any("github installation ref cleared" in r.getMessage() for r in caplog.records)
