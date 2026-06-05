"""GitHub App Manifest flow helpers (Lift 1.5).

The manifest flow lets bsvibe create its own GitHub App: the founder POSTs a
manifest to ``github.com/settings/apps/new``, approves, and GitHub redirects
back with a short-lived ``code`` that we exchange at
``/app-manifests/{code}/conversions`` for the full credential set (app_id,
client_id/secret, private key PEM, webhook secret). No manual env editing.

This module builds the manifest body and performs the code→credentials
exchange. Storage + provider registration live in the endpoint layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

_MANIFEST_POST_URL = "https://github.com/settings/apps/new"
_CONVERSIONS_URL = "https://api.github.com/app-manifests/{code}/conversions"
_HTTP_TIMEOUT = 10.0

# Least-privilege defaults for what bsvibe does today: open PRs against the
# bound repo (contents + pull_requests write, metadata read).
_DEFAULT_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "metadata": "read",
}
_DEFAULT_EVENTS = ["push", "pull_request"]


@dataclass(frozen=True)
class GitHubAppManifestResult:
    """The credential set GitHub mints when an App is created from a manifest."""

    app_id: str
    client_id: str
    client_secret: str
    private_key_pem: str
    app_slug: str | None = None
    webhook_secret: str | None = None
    html_url: str | None = None


def manifest_post_url(state: str) -> str:
    """The GitHub endpoint the PWA auto-submits the manifest form to."""
    return f"{_MANIFEST_POST_URL}?state={state}"


def build_manifest(
    *, homepage_url: str, redirect_url: str, oauth_callback_url: str, webhook_url: str
) -> dict[str, Any]:
    """Build the GitHub App manifest body (the PWA posts it as ``manifest``).

    ``redirect_url`` is where GitHub returns the creation ``code``;
    ``oauth_callback_url`` is the user-to-server OAuth callback ("Connect with
    GitHub"). Both must be the CONFIGURED external URLs (never request-derived).
    """
    return {
        "url": homepage_url,
        "redirect_url": redirect_url,
        "callback_urls": [oauth_callback_url],
        "hook_attributes": {"url": webhook_url},
        "public": False,
        "default_permissions": dict(_DEFAULT_PERMISSIONS),
        "default_events": list(_DEFAULT_EVENTS),
    }


async def convert_manifest_code(code: str) -> GitHubAppManifestResult:
    """Exchange a manifest ``code`` for the minted App credentials."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            _CONVERSIONS_URL.format(code=code),
            headers={"Accept": "application/vnd.github+json"},
        )
    resp.raise_for_status()
    data = resp.json()
    return GitHubAppManifestResult(
        app_id=str(data["id"]),
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        private_key_pem=data["pem"],
        app_slug=data.get("slug"),
        webhook_secret=data.get("webhook_secret"),
        html_url=data.get("html_url"),
    )


__all__ = [
    "GitHubAppManifestResult",
    "build_manifest",
    "convert_manifest_code",
    "manifest_post_url",
]
