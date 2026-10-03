"""The per-run token ceiling a founder can extend (#1105).

The base ceiling is ``agent_max_run_tokens`` (deployment-wide). When a run stops
on it, the founder's "keep going" GRANTS that run more: one more full ceiling on
top of what it had already used, recorded on the run's payload. Usage accumulates
on the run, so without the grant a resumed run would cross the base ceiling again
on its first turn — which is why the checkpoint had no retry before.

Pure and leaf-level: both enforcement points read it — the loop's after-turn
check (``token_budget``) and the in-session budget the adapter sends the worker
(``ExecutorAdapter._session_limits``) — and so does the checkpoint resolve that
writes the grant, which MCP reaches and which must not import the loop graph.
"""

from __future__ import annotations

from typing import Any

TOKEN_CAP_DECISION_KIND = "run_token_cap_reached"  # noqa: S105 — decision kind, not a secret
#: ``run.payload`` key holding the run's granted absolute ceiling.
TOKEN_CAP_GRANT_KEY = "token_cap_granted"  # noqa: S105 — payload key, not a secret


def effective_run_token_cap(base_cap: int, payload: Any) -> int:
    """The ceiling that applies to this run: the base, or the founder's grant if higher.

    ``base_cap <= 0`` means the ceiling is switched off deployment-wide and stays off —
    a grant extends a ceiling, it never creates one.
    """
    if base_cap <= 0:
        return base_cap
    granted = payload.get(TOKEN_CAP_GRANT_KEY) if isinstance(payload, dict) else None
    if isinstance(granted, int) and not isinstance(granted, bool) and granted > base_cap:
        return granted
    return base_cap


def granted_cap_after(used: int, base_cap: int) -> int:
    """The new ceiling when the founder says "keep going": one more full base ceiling."""
    return used + base_cap


__all__ = [
    "TOKEN_CAP_DECISION_KIND",
    "TOKEN_CAP_GRANT_KEY",
    "effective_run_token_cap",
    "granted_cap_after",
]
