"""The gated per-product test-PG sidecar settings — default OFF + dict parses.

The gate defaults to OFF and the freeform ``dict[str, str]`` env field accepts
a literal default and a JSON env override (pydantic-settings decodes complex
types from env as JSON).
"""

from __future__ import annotations

import pytest

from backend.config import Settings


def test_test_db_gate_defaults_off() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.sandbox_test_db_enabled is False
    assert s.sandbox_test_db_setup_cmd == ""
    assert s.sandbox_test_db_ready_timeout_s == 60.0


def test_test_db_env_dict_default_is_accepted() -> None:
    """The ``dict[str, str]`` field parses its literal default (mirrors CI)."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    env = s.sandbox_test_db_env
    assert isinstance(env, dict)
    assert "{host}" in env["BSVIBE_DATABASE_URL"]
    assert env["BSVIBE_APP_DB_PASSWORD"] == "bsvibe_app_ci"
    assert env["BSVIBE_MIGRATION_DATABASE_URL"].startswith("postgresql+asyncpg://bsvibe:bsvibe@")


def test_test_db_env_overridable_via_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env override form is JSON (pydantic-settings complex-type decoding)."""
    monkeypatch.setenv(
        "BSVIBE_SANDBOX_TEST_DB_ENV",
        '{"BSVIBE_DATABASE_URL": "postgresql+asyncpg://u:p@{host}:5432/db"}',
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.sandbox_test_db_env == {
        "BSVIBE_DATABASE_URL": "postgresql+asyncpg://u:p@{host}:5432/db"
    }


def test_test_db_gate_enable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BSVIBE_SANDBOX_TEST_DB_ENABLED", "true")
    monkeypatch.setenv("BSVIBE_SANDBOX_TEST_DB_SETUP_CMD", "uv run alembic upgrade head")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.sandbox_test_db_enabled is True
    assert s.sandbox_test_db_setup_cmd == "uv run alembic upgrade head"
