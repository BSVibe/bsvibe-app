"""Workflow application — per-stage services.

Each module is the application service for one stage of the v8 §7
workflow (Receive → Frame → Run → Verify → Settle → Deliver). H2b
absorbs the legacy ``backend.orchestrator.frame.FrameStage`` here as
``stages.frame``. H3 will add the remaining stage services
(``receive``, ``settle``, ``deliver``) when ``intake/`` and
``delivery/`` move into the Workflow context.
"""

from __future__ import annotations
