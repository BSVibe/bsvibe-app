"""Path and id helpers for canonicalization notes (Handoff §1, §2)."""

from __future__ import annotations

import re
from datetime import datetime

# Per Handoff §2: ^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$
_CONCEPT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

# Per Handoff §6, §7
_ACTION_KIND_DIRS = {
    "create-concept",
    "merge-concepts",
    "split-concept",
    "deprecate-concept",
    "restore-concept",
    "retag-notes",
    "update-policy",
    "create-decision",
}

# Per Handoff §5
PROPOSAL_KINDS: frozenset[str] = frozenset(
    {
        "merge-concepts",
        "create-concept",
        "retag-notes",
        "policy-update",
        "policy-conflict",
        "decision-review",
    }
)

# Per Handoff §8.1
DECISION_KINDS: frozenset[str] = frozenset({"cannot-link", "must-link"})

# Per Handoff §8.2
POLICY_KINDS: frozenset[str] = frozenset({"staleness", "merge-auto-apply", "decision-maturity"})


def is_valid_concept_id(concept_id: str) -> bool:
    """Return True if the string matches the concept id regex (Handoff §2)."""
    if not concept_id:
        return False
    return _CONCEPT_ID_RE.match(concept_id) is not None


def validate_concept_id(concept_id: str) -> str:
    """Return the id unchanged if valid; raise ValueError otherwise."""
    if not is_valid_concept_id(concept_id):
        msg = f"invalid concept id: {concept_id!r}"
        raise ValueError(msg)
    return concept_id


def format_action_timestamp(dt: datetime) -> str:
    """Format a datetime as ``YYYYMMDD-HHMMSS`` for action filenames."""
    return dt.strftime("%Y%m%d-%H%M%S")


def build_action_filename(dt: datetime, slug: str) -> str:
    """Construct ``YYYYMMDD-HHMMSS-<slug>.md`` for action notes (Handoff §2).

    Slug must follow the same character rules as concept ids.
    """
    if not is_valid_concept_id(slug):
        msg = f"invalid slug for action filename: {slug!r}"
        raise ValueError(msg)
    return f"{format_action_timestamp(dt)}-{slug}.md"


def build_action_path(action_kind: str, dt: datetime, slug: str) -> str:
    """Construct ``actions/<kind>/<filename>`` (Handoff §6)."""
    if action_kind not in _ACTION_KIND_DIRS:
        msg = f"unknown action kind: {action_kind!r}"
        raise ValueError(msg)
    return f"actions/{action_kind}/{build_action_filename(dt, slug)}"


def with_collision_suffix(rel_path: str, existing: set[str]) -> str:
    """Apply deterministic ``-02``, ``-03``, ... suffix if path is taken (Handoff §2).

    ``rel_path`` is expected to end in ``.md``. ``existing`` is the set of
    vault-relative paths already present.
    """
    if rel_path not in existing:
        return rel_path
    if not rel_path.endswith(".md"):
        msg = f"path must end with .md: {rel_path!r}"
        raise ValueError(msg)
    stem = rel_path[: -len(".md")]
    n = 2
    while True:
        candidate = f"{stem}-{n:02d}.md"
        if candidate not in existing:
            return candidate
        n += 1


def active_concept_path(concept_id: str) -> str:
    """Vault-relative path for an active concept (Handoff §3.1)."""
    validate_concept_id(concept_id)
    return f"concepts/active/{concept_id}.md"


def merged_concept_path(concept_id: str) -> str:
    """Vault-relative path for a merged-concept tombstone (Handoff §3.2)."""
    validate_concept_id(concept_id)
    return f"concepts/merged/{concept_id}.md"


def deprecated_concept_path(concept_id: str) -> str:
    """Vault-relative path for a deprecated concept (Handoff §3.3)."""
    validate_concept_id(concept_id)
    return f"concepts/deprecated/{concept_id}.md"


def build_proposal_path(proposal_kind: str, dt: datetime, slug: str) -> str:
    """Construct ``proposals/<kind>/<filename>`` (Handoff §5)."""
    if proposal_kind not in PROPOSAL_KINDS:
        msg = f"unknown proposal kind: {proposal_kind!r}"
        raise ValueError(msg)
    return f"proposals/{proposal_kind}/{build_action_filename(dt, slug)}"


def build_decision_path(decision_kind: str, dt: datetime, slug: str) -> str:
    """Construct ``decisions/<kind>/<filename>`` (Handoff §8.1)."""
    if decision_kind not in DECISION_KINDS:
        msg = f"unknown decision kind: {decision_kind!r}"
        raise ValueError(msg)
    return f"decisions/{decision_kind}/{build_action_filename(dt, slug)}"


def build_create_decision_action_path(decision_kind: str, dt: datetime, slug: str) -> str:
    """Construct ``actions/create-decision/<kind>/<filename>`` (Handoff §7.8)."""
    if decision_kind not in DECISION_KINDS:
        msg = f"unknown decision kind: {decision_kind!r}"
        raise ValueError(msg)
    return f"actions/create-decision/{decision_kind}/{build_action_filename(dt, slug)}"


def build_policy_path(policy_kind: str, profile_name: str) -> str:
    """Construct ``decisions/policy/<kind>/<profile>.md`` (Handoff §8.2)."""
    if policy_kind not in POLICY_KINDS:
        msg = f"unknown policy kind: {policy_kind!r}"
        raise ValueError(msg)
    if not is_valid_concept_id(profile_name):
        msg = f"invalid policy profile name: {profile_name!r}"
        raise ValueError(msg)
    return f"decisions/policy/{policy_kind}/{profile_name}.md"
