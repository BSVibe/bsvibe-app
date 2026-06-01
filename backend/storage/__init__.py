"""Storage seams.

Currently houses :mod:`backend.storage.artifact_store` — the per-run
artifact storage Protocol + the local filesystem implementation that
backs it today. Future R2/S3 implementations land here and satisfy the
same Protocol (swap-ready surface, no call-site rewrite).
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
