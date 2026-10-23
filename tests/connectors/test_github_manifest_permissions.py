"""Guard: the GitHub App manifest requests every permission delivery's writes need."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from structlog.testing import capture_logs

from backend.connectors.auth.github_manifest import build_manifest
from backend.workflow.application.delivery.connector_dispatch import _github_pr

_CLIENT = Path(__file__).resolve().parents[2] / "plugin" / "github" / "client.py"
_WRITE_CALL = re.compile(
    r'_request\(\s*"(POST|PUT|PATCH|DELETE)",\s*f"/repos/\{owner\}/\{repo\}/([^"?]+)"'
)
_LEVEL = {"read": 1, "write": 2}


def _permission_for(path: str) -> str:
    if path.startswith("pulls"):
        return "pull_requests"
    if path.startswith("issues"):
        return "issues"
    return "contents"


def _manifest_permissions() -> dict[str, str]:
    manifest = build_manifest(
        homepage_url="https://x",
        redirect_url="https://x/r",
        oauth_callback_url="https://x/o",
        webhook_url="https://x/w",
    )
    return manifest["default_permissions"]


def test_manifest_covers_every_github_write_endpoint() -> None:
    calls = _WRITE_CALL.findall(_CLIENT.read_text())
    assert calls, "no write calls found — the regex drifted from plugin/github/client.py"
    granted = _manifest_permissions()
    missing = {
        f"{method} /{path}": _permission_for(path)
        for method, path in calls
        if _LEVEL.get(granted.get(_permission_for(path), ""), 0) < _LEVEL["write"]
    }
    assert not missing, f"manifest lacks write permission for: {missing}"


def test_issue_comment_is_among_the_guarded_writes() -> None:
    calls = _WRITE_CALL.findall(_CLIENT.read_text())
    assert any(m == "POST" and p.endswith("/comments") for m, p in calls)
    assert _manifest_permissions()["issues"] == "write"


def test_permission_denied_warns_once_per_workspace() -> None:
    exc = RuntimeError("Client error '403 Forbidden' Resource not accessible by integration")
    assert _github_pr._is_issues_permission_denied(exc)
    assert not _github_pr._is_issues_permission_denied(RuntimeError("500 boom"))
    ws = uuid.uuid4()
    with capture_logs() as logs:
        _github_pr._warn_issues_write_missing_once(ws, "o/r", 1)
        _github_pr._warn_issues_write_missing_once(ws, "o/r", 2)
    events = [e["event"] for e in logs]
    assert events == ["github_app_issues_write_permission_missing"]
