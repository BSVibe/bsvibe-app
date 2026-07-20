"""Executor pool — host-installed CLI worker + server-side dispatch.

Namespace-only. The headless client lives under
:mod:`backend.executors.worker`; the server-side dispatch primitives in
:mod:`backend.executors.dispatch` and the worker registry / heartbeat in
:mod:`backend.executors.service` / :mod:`backend.executors.db`.
Lift N defensive pattern #1.
"""

from __future__ import annotations

__all__: list[str] = []
