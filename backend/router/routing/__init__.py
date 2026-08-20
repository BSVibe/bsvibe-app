"""Routing domain — the RUN routing rules the founder actually writes.

Bundle 1.5b's LLM-provider routing (catalog / logs / registry / strategies)
lived here and was deleted 2026-08-20: it had zero production callers and its
two tables held zero rows in prod. ``strategies.py`` had said so itself —
*"Wired into the dispatch path in Bundle 1.5c when the LiteLLM hook lands"* —
and the directory that hook was to land in never received a single ``.py``.
Routing flows through :mod:`backend.dispatch` instead, per the founder policy
``bsvibe-no-implicit-routing``.

What remains is :mod:`.run_routing` — the rule engine behind
``run_routing_rules``, which is live. It is imported directly by its callers;
this package root deliberately re-exports nothing, so a future reader cannot
mistake a re-export for a public surface the way the deleted one was mistaken
for a wired subsystem.
"""

from __future__ import annotations

__all__: list[str] = []
