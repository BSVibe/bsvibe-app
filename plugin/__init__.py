"""Repo-root plugin namespace (Lift R1 + D38).

BSVibe plugin implementations live at ``plugin/<name>/`` per v8 §D38. Each
plugin is a self-contained package that imports only from :mod:`bsvibe_sdk`
(plus a small allow-list of backend leaf modules — see PR body).

The backend extension engine discovers plugins by *path scan* via
:class:`backend.extensions.plugin.loader.PluginLoader`, NOT by Python
import. Listing this directory as a package keeps direct ``plugin.<name>``
imports available for callers inside the repo (today: webhook parsers
imported by ``backend.connectors.resolver`` and ``backend.api.webhooks``;
flagged in PR body as a follow-up direction-of-dependency fix).
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
