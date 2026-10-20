"""Where a run records its off-machine checkpoint (#929, B).

A leaf, so cleanup code can name the record without importing the checkpoint policy (which
reaches the account resolution and, through it, other contexts).
"""

from __future__ import annotations

#: ``run.payload`` key holding the last checkpoint: ``{"sha", "machine"[, "stale"]}``.
CHECKPOINT_KEY = "checkpoint"

__all__ = ["CHECKPOINT_KEY"]
