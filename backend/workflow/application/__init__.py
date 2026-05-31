"""Workflow context — application layer.

H1 placeholder. H2 decomposes ``execution/orchestrator.py`` into
``agent_loop.py``, ``tool_registry.py``, ``connector_action_registrar.py``,
``run_persistence.py``; H3 absorbs ``intake/`` + ``delivery/`` into
``application/stages/``.

D36 invariant — external callers MUST import from this module only
(once H2 + H3 populate the public surface). The 13 public functions
listed in v8 §7.5 land in H2/H3.
"""

from __future__ import annotations
