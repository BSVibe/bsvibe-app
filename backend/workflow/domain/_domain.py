from __future__ import annotations

import enum


class RequestStatus(str, enum.Enum):
    open = "open"
    running = "running"
    needs_decision = "needs_decision"
    review_ready = "review_ready"
    shipped = "shipped"
    abandoned = "abandoned"


class WorkStepStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    needs_decision = "needs_decision"
    verifying = "verifying"
    review_ready = "review_ready"
    failed = "failed"
    skipped = "skipped"


class ProofState(str, enum.Enum):
    verification_missing = "verification_missing"
    verifying = "verifying"
    verified = "verified"
    verification_failed = "verification_failed"
    human_review_required = "human_review_required"
