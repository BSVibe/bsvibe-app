from __future__ import annotations

import enum


class DirectionSource(str, enum.Enum):
    web = "web"
    mobile_web = "mobile_web"
    slack = "slack"
    email = "email"
    cli = "cli"
    voice = "voice"


class RequestStatus(str, enum.Enum):
    open = "open"
    running = "running"
    needs_decision = "needs_decision"
    review_ready = "review_ready"
    shipped = "shipped"
    abandoned = "abandoned"


class WorkPlanStatus(str, enum.Enum):
    draft = "draft"
    active = "active"
    superseded = "superseded"
    completed = "completed"


class WorkPlanCreatedBy(str, enum.Enum):
    system = "system"
    llm_assisted = "llm_assisted"
    user = "user"


class WorkStepStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    needs_decision = "needs_decision"
    verifying = "verifying"
    review_ready = "review_ready"
    failed = "failed"
    skipped = "skipped"


class RunAttemptPhase(str, enum.Enum):
    prepare = "prepare"
    work = "work"
    verify = "verify"
    summarize = "summarize"
    terminal = "terminal"


class RunAttemptStatus(str, enum.Enum):
    running = "running"
    completed = "completed"
    failed = "failed"
    timed_out = "timed_out"


class DeliverableType(str, enum.Enum):
    code = "code"
    pr = "pr"
    preview = "preview"
    design = "design"
    doc = "doc"
    data = "data"
    marketing = "marketing"
    report = "report"


class DeliverableStatus(str, enum.Enum):
    draft = "draft"
    verifying = "verifying"
    review_ready = "review_ready"
    shipped = "shipped"
    rejected = "rejected"


class ProofState(str, enum.Enum):
    verification_missing = "verification_missing"
    verifying = "verifying"
    verified = "verified"
    verification_failed = "verification_failed"
    human_review_required = "human_review_required"


class ProofAttemptStatus(str, enum.Enum):
    """DEPRECATED: kept only for old ProofAttempt rows during the
    multi-aspect migration. New code should use ``ProofAspectStatus``.
    Remove once all consumers have migrated."""

    queued = "queued"
    running = "running"
    verified = "verified"
    failed = "failed"
    human_review_required = "human_review_required"


class ProofAspectType(str, enum.Enum):
    """A discrete verification dimension. Each deliverable can have N
    aspects; the deliverable is ``verified`` iff every blocking aspect
    ``passed``. Adding a new aspect (security audit, knowledge check,
    marketing copy fact-check) is additive — no rework of the roll-up
    or the rest of the pipeline."""

    code_test = "code_test"
    code_lint = "code_lint"
    code_install_smoke = "code_install_smoke"
    code_build = "code_build"
    # Verification Contract aspects (2026-05-17). ``declared_command``
    # is one ``command`` check from the work LLM's declared contract;
    # ``llm_judge`` is one ``judge`` check (LLM-graded rubric, P2).
    # These supersede the heuristic ``code_*`` aspects above.
    declared_command = "declared_command"
    llm_judge = "llm_judge"


class ProofAspectStatus(str, enum.Enum):
    """Per-aspect lifecycle. ``passed`` / ``failed`` are the verdicts
    the roll-up uses; ``error`` means the infra itself broke and the
    aspect can't pronounce a verdict (so the deliverable falls to
    ``human_review_required`` instead of being penalised for our
    own bug). ``skipped`` is reserved for opt-in cases — most
    not-applicable aspects are simply *absent*, not skipped."""

    queued = "queued"
    running = "running"
    passed = "passed"
    failed = "failed"
    skipped = "skipped"
    error = "error"


class BriefScope(str, enum.Enum):
    company = "company"
    project = "project"
    request = "request"
