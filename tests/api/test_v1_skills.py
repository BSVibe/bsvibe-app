"""End-to-end test for /api/v1/skills against a real SkillLoader."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.deps import get_current_user, get_workspace_id
from backend.api.main import create_app
from backend.config import get_settings

from .._support import fake_current_user


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def configured_app(tmp_path: Path, workspace_id: uuid.UUID):
    """Build an app with skills_root pointed at tmp + workspace_id dep override."""
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    get_settings.cache_clear()  # ensure our env overrides take effect
    app.state._skills_root_override = tmp_path  # for test introspection if needed
    return app


def _write_skill(root: Path, name: str, version: str = "1.0", description: str = "desc") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(
        f"---\nname: {name}\nversion: {version}\ndescription: {description}\n---\nbody\n",
        encoding="utf-8",
    )


def test_list_skills_empty(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    client = TestClient(configured_app)
    r = client.get("/api/v1/skills")
    assert r.status_code == 200
    assert r.json() == []


def test_list_skills_returns_workspace_skills(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    _write_skill(tmp_path / str(workspace_id), "weekly-digest", "1.0.0", "Weekly digest skill.")
    _write_skill(tmp_path / str(workspace_id), "insight-linker", "2.1.0", "Cross-link insights.")
    # Other workspace's skills MUST NOT leak.
    _write_skill(tmp_path / str(uuid.uuid4()), "other-skill", "1.0", "should not appear")

    client = TestClient(configured_app)
    r = client.get("/api/v1/skills")
    assert r.status_code == 200
    names = sorted(s["name"] for s in r.json())
    assert names == ["insight-linker", "weekly-digest"]


def test_get_skill_by_name(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    _write_skill(tmp_path / str(workspace_id), "weekly-digest", "1.0.0", "Weekly digest.")
    client = TestClient(configured_app)
    r = client.get("/api/v1/skills/weekly-digest")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "weekly-digest"
    assert body["version"] == "1.0.0"
    assert body["has_system_prompt"] is True


def test_get_skill_not_found(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    client = TestClient(configured_app)
    r = client.get("/api/v1/skills/missing")
    assert r.status_code == 404


def test_settings_override_is_isolated(monkeypatch) -> None:
    # Belt-and-suspenders: ensure get_settings cache flips with the env.
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", "/tmp/x-test-isolated")
    get_settings.cache_clear()
    assert get_settings().skills_root == "/tmp/x-test-isolated"
    get_settings.cache_clear()


def teardown_module() -> None:
    """Restore the cached settings after this module's tests."""
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings_cache_after_each_test():
    yield
    get_settings.cache_clear()
