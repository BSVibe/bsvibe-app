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


def test_create_skill_round_trips_through_loader(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    """POST writes a .md the loader then lists + serves (full round-trip)."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    client = TestClient(configured_app)

    r = client.post(
        "/api/v1/skills",
        json={
            "name": "blog-writer",
            "summary": "Drafts a technical blog post in the house voice.",
            "system_prompt": "You write calm, precise technical prose.",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "blog-writer"
    assert body["description"] == "Drafts a technical blog post in the house voice."
    assert body["has_system_prompt"] is True

    # The file landed under the per-workspace skills dir.
    md_path = tmp_path / str(workspace_id) / "blog-writer.md"
    assert md_path.is_file()

    # The loader-backed GET list now includes it.
    listed = client.get("/api/v1/skills")
    assert listed.status_code == 200
    assert "blog-writer" in [s["name"] for s in listed.json()]

    # And GET /{name} serves the freshly-written skill.
    one = client.get("/api/v1/skills/blog-writer")
    assert one.status_code == 200
    assert one.json()["name"] == "blog-writer"
    assert one.json()["has_system_prompt"] is True


def test_create_skill_derives_slug_from_name(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    """A human-friendly name is slugified for the filename + manifest name."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    client = TestClient(configured_app)

    r = client.post(
        "/api/v1/skills",
        json={"name": "Weekly Digest", "summary": "Sum up the week.", "system_prompt": "Go."},
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "weekly-digest"
    assert (tmp_path / str(workspace_id) / "weekly-digest.md").is_file()


def test_create_skill_duplicate_name_conflicts(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    """A second create with the same slug → 409, original untouched."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    _write_skill(tmp_path / str(workspace_id), "blog-writer", "1.0", "Existing.")
    client = TestClient(configured_app)

    r = client.post(
        "/api/v1/skills",
        json={"name": "blog-writer", "summary": "New.", "system_prompt": "x"},
    )
    assert r.status_code == 409, r.text


def test_create_skill_empty_name_rejected(configured_app, tmp_path: Path, monkeypatch) -> None:
    """An empty / unslugifiable name → 422 (no skill written)."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    client = TestClient(configured_app)

    r = client.post(
        "/api/v1/skills",
        json={"name": "   ", "summary": "s", "system_prompt": "p"},
    )
    assert r.status_code == 422, r.text


def test_create_skill_extra_field_forbidden(configured_app, tmp_path: Path, monkeypatch) -> None:
    """extra=forbid: an unknown body field → 422."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    client = TestClient(configured_app)

    r = client.post(
        "/api/v1/skills",
        json={"name": "ok-skill", "summary": "s", "system_prompt": "p", "version": "9.9"},
    )
    assert r.status_code == 422, r.text


def test_create_skill_path_traversal_slug_rejected(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    """A name that would escape the workspace dir is sanitized / rejected — the
    write MUST stay inside <skills_root>/<workspace_id>/ (no traversal)."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    client = TestClient(configured_app)

    r = client.post(
        "/api/v1/skills",
        json={"name": "../../etc/passwd", "summary": "s", "system_prompt": "p"},
    )
    # Either rejected outright (422) — the only outcome that is safe.
    assert r.status_code == 422, r.text
    # Nothing escaped the workspace dir.
    assert not (tmp_path.parent / "etc").exists()
    assert not (tmp_path / "etc").exists()


def test_create_skill_blank_summary_rejected(configured_app, tmp_path: Path, monkeypatch) -> None:
    """summary becomes the manifest description (the LLM match signal) — blank → 422."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    client = TestClient(configured_app)

    r = client.post(
        "/api/v1/skills",
        json={"name": "ok-skill", "summary": "   ", "system_prompt": "p"},
    )
    assert r.status_code == 422, r.text


def test_get_skill_returns_system_prompt_body(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    """GET /{name} carries the raw system-prompt body (needed to edit it)."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    root = tmp_path / str(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "blog-writer.md").write_text(
        "---\nname: blog-writer\nversion: 1.0.0\ndescription: Drafts a post.\n---\n"
        "You write calm, precise prose.\n",
        encoding="utf-8",
    )
    client = TestClient(configured_app)
    r = client.get("/api/v1/skills/blog-writer")
    assert r.status_code == 200
    assert r.json()["system_prompt"] == "You write calm, precise prose."


def test_update_skill_persists_summary_and_system_prompt(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    """PATCH updates description + body; the round-tripped GET reflects both."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    _write_skill(tmp_path / str(workspace_id), "blog-writer", "1.0.0", "Old summary.")
    client = TestClient(configured_app)

    r = client.patch(
        "/api/v1/skills/blog-writer",
        json={"summary": "New summary line.", "system_prompt": "New prompt body."},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "blog-writer"
    assert body["description"] == "New summary line."
    assert body["system_prompt"] == "New prompt body."
    assert body["has_system_prompt"] is True

    # Persisted: a fresh GET reflects the edit.
    one = client.get("/api/v1/skills/blog-writer")
    assert one.json()["description"] == "New summary line."
    assert one.json()["system_prompt"] == "New prompt body."


def test_update_skill_keeps_name_immutable(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    """The slug/filename never changes — only the body fields are mutable."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    _write_skill(tmp_path / str(workspace_id), "blog-writer", "1.0.0", "Old.")
    client = TestClient(configured_app)

    r = client.patch(
        "/api/v1/skills/blog-writer",
        json={"summary": "New.", "system_prompt": "Body."},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "blog-writer"
    assert (tmp_path / str(workspace_id) / "blog-writer.md").is_file()


def test_update_skill_not_found(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    """PATCH on a skill that isn't loaded → 404 (no file written)."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    client = TestClient(configured_app)

    r = client.patch(
        "/api/v1/skills/missing",
        json={"summary": "s", "system_prompt": "p"},
    )
    assert r.status_code == 404, r.text
    assert not (tmp_path / str(workspace_id) / "missing.md").exists()


def test_update_skill_blank_summary_rejected(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    """summary is the LLM match signal — a blank update → 422, file untouched."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    _write_skill(tmp_path / str(workspace_id), "blog-writer", "1.0.0", "Keep me.")
    client = TestClient(configured_app)

    r = client.patch(
        "/api/v1/skills/blog-writer",
        json={"summary": "   ", "system_prompt": "p"},
    )
    assert r.status_code == 422, r.text
    # Original untouched.
    assert client.get("/api/v1/skills/blog-writer").json()["description"] == "Keep me."


def test_update_skill_extra_field_forbidden(
    configured_app, tmp_path: Path, workspace_id: uuid.UUID, monkeypatch
) -> None:
    """extra=forbid: an unknown body field (e.g. trying to rename) → 422."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    _write_skill(tmp_path / str(workspace_id), "blog-writer", "1.0.0", "Old.")
    client = TestClient(configured_app)

    r = client.patch(
        "/api/v1/skills/blog-writer",
        json={"summary": "s", "system_prompt": "p", "name": "renamed"},
    )
    assert r.status_code == 422, r.text


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
