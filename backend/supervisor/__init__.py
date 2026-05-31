"""BSVibe supervisor — sandbox script execution only (post-Lift G).

Audit (formerly ``backend.supervisor.audit``) moved to
``backend.extensions.implementations.audit`` in Lift G as the first
concrete extension implementation. ``backend/supervisor/`` now wraps the
DinD-backed script runner only. Lift H folds sandbox into Verifier; this
directory disappears at that point.
"""

from __future__ import annotations

from backend.supervisor import sandbox

__all__ = ["sandbox"]
