"""H3 — the runtime "set deployment-global provider App creds" write is gone.

App credentials are deployment-global (one row per instance, no workspace_id),
but the only role vocabulary is per-workspace, so any authenticated member could
overwrite the whole instance's slack/notion/discord OAuth App secret — a
tenant→instance escalation. There is no operator identity to gate it by; the
operator path is env (``register_configured_providers``). The REST route, the MCP
tool, and the service wrapper are all removed. github's manifest flow (which uses
``upsert_app_credentials`` directly) is untouched — the positive control.
"""

from __future__ import annotations

from backend.api.main import create_app
from backend.connectors.auth import service


def test_service_wrapper_is_gone() -> None:
    assert not hasattr(service, "set_app_credentials")


def test_rest_route_is_gone() -> None:
    app = create_app()
    paths = {r.path for r in app.routes}  # type: ignore[attr-defined]
    assert "/api/v1/connectors/oauth/{provider}/app-credentials" not in paths


def test_github_manifest_upsert_survives() -> None:
    """Positive control — the deployment-global write github legitimately uses
    (via its authenticated manifest callback) must still exist."""
    from backend.connectors.auth.app_credentials import upsert_app_credentials

    assert callable(upsert_app_credentials)


def test_env_registration_path_survives() -> None:
    """Positive control — the operator path (env) is intact."""
    from backend.connectors.auth.bootstrap import register_configured_providers

    assert callable(register_configured_providers)
