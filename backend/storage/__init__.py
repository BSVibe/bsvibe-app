"""Storage seams.

Currently houses :mod:`backend.storage.artifact_store` — the per-run
artifact storage Protocol + the local filesystem implementation that
backs it today. Future R2/S3 implementations land here and satisfy the
same Protocol (swap-ready surface, no call-site rewrite).
"""
