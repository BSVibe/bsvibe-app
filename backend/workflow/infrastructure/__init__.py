"""Workflow context — infrastructure layer.

H1 hosts the advisory-lock primitive used by ``RunOrchestrator`` to
prevent double-dispatch across uvicorn instances (v3 D15). H2/H3 add
repositories + worker entry points.
"""

from __future__ import annotations
