"""Executor pool — host-installed CLI worker + server-side dispatch.

Namespace-only. The headless client lives under
:mod:`backend.executors.worker`; the server-side dispatch / orchestration
in :mod:`backend.executors.dispatch` / :mod:`backend.executors.orchestrator`.
Lift N defensive pattern #1.
"""

from __future__ import annotations

__all__: list[str] = []
