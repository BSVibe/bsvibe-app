"""PolicyResolver — slice 4 active-policy selection (Handoff §8.2).

Implements the resolver order from §8.2:
1. Candidate path category matches requested policy kind.
2. status: active.
3. scope matches current action/context.
4. valid_from <= now.
5. expires_at is absent/null or now < expires_at.
6. If exactly one candidate remains, select it.
7. If multiple candidates remain, select highest priority.
8. If priority ties → ``policy_conflict`` (raised as ``PolicyConflictError``).

Default profiles (Handoff §8.3-8.5) are written by ``bootstrap_defaults``;
re-running is idempotent (does not overwrite existing notes).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.knowledge.canonicalization import models, paths
from backend.knowledge.canonicalization.index import CanonicalizationIndex
from backend.knowledge.canonicalization.store import NoteStore

logger = structlog.get_logger(__name__)

_DEFAULT_PROFILE = "conservative-default"
_GENERATOR_VERSION = "canonicalization-policy-bootstrap-v1"


class PolicyConflictError(RuntimeError):
    """Multiple equally-priority active policies match — surface as Hard Block."""


# --------------------------------------------------------- default policy bodies

# Per Handoff §8.3
_DEFAULT_STALENESS_PARAMS: dict[str, Any] = {
    "proposal_ttl": {
        "merge-concepts": "7d",
        "create-concept": "14d",
        "retag-notes": "3d",
        "policy-update": "14d",
    },
    "draft_action_ttl": {
        "merge-concepts": "24h",
        "create-concept": "7d",
        "retag-notes": "24h",
        "policy-update": "7d",
    },
    "freshness": {
        "require_source_proposal_pending": True,
        "require_policy_profile_match": True,
        "require_validation_rerun_before_apply": True,
        "require_scoring_rerun_before_apply": True,
    },
}

# Per Handoff §8.4
_DEFAULT_DECISION_MATURITY_PARAMS: dict[str, Any] = {
    "thresholds": {
        "hard_block": 0.85,
        "review": 0.60,
    },
    "promotion": {
        "budding_min_confirmations": 2,
        "evergreen_min_confirmations": 3,
        "evergreen_min_days_stable": 14,
    },
    "demotion": {
        "demote_evergreen_below_hard_block": True,
        "demote_budding_below_review": True,
        "demote_on_strong_conflict": True,
    },
}

# Per Handoff §8.5
_DEFAULT_MERGE_AUTO_APPLY_PARAMS: dict[str, Any] = {
    "safe_mode_on": {
        "auto_apply_threshold": 0.90,
        "auto_action_kinds": [
            "create-concept",
            "retag-notes",
            "merge-concepts",
        ],
        "max_affected_paths": {
            "create-concept": 2,
            "retag-notes": 5,
            "merge-concepts": 10,
        },
    },
    "safe_mode_off": {"bypass_approval": True},
    "hard_blocks": {"cannot_link_threshold": 0.85},
}

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "staleness": _DEFAULT_STALENESS_PARAMS,
    "decision-maturity": _DEFAULT_DECISION_MATURITY_PARAMS,
    "merge-auto-apply": _DEFAULT_MERGE_AUTO_APPLY_PARAMS,
}

_DEFAULT_SCHEMA_VERSIONS: dict[str, str] = {
    "staleness": "staleness-policy-v1",
    "decision-maturity": "decision-maturity-policy-v1",
    "merge-auto-apply": "merge-auto-apply-policy-v1",
}

_DEFAULT_PRIORITY = 100


class PolicyResolver:
    """Active-policy selector + default fixture bootstrap (Handoff §8.2)."""

    def __init__(
        self,
        index: CanonicalizationIndex,
        store: NoteStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._index = index
        self._store = store
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    async def bootstrap_defaults(self) -> list[str]:
        """Idempotently write the three default policy notes (§8.3-8.5).

        Returns the list of paths created during this call (already-present
        files are skipped). Per §8.2, policy SoT lives in the vault; this
        method is just a deterministic way to initialise an empty vault.
        """
        created: list[str] = []
        now = self._clock()
        for kind in ("staleness", "merge-auto-apply", "decision-maturity"):
            path = paths.build_policy_path(kind, _DEFAULT_PROFILE)
            if await self._store.read_policy(path) is not None:
                continue
            entry = models.PolicyEntry(
                path=path,
                kind=kind,
                status="active",
                profile_name=_DEFAULT_PROFILE,
                priority=_DEFAULT_PRIORITY,
                scope={},
                policy_schema_version=_DEFAULT_SCHEMA_VERSIONS[kind],
                valid_from=now,
                params=_DEFAULT_PARAMS[kind],
                created_at=now,
                updated_at=now,
                learned_from={
                    "telemetry_snapshot": None,
                    "plugin": _GENERATOR_VERSION,
                },
            )
            await self._store.write_policy(entry)
            await self._index.invalidate(path)
            created.append(path)
        return created

    async def select(self, *, kind: str, scope: dict[str, Any]) -> models.PolicyEntry | None:
        """Return the active policy for ``kind`` matching ``scope``.

        Raises ``PolicyConflictError`` when two equally-prioritised active
        policies match (per §8.2 step 8 — do not silently choose).
        """
        now = _aware(self._clock())
        candidates = await self._index.list_policies(kind=kind, status="active")
        matched: list[models.PolicyEntry] = []
        for p in candidates:
            if _aware(p.valid_from) > now:
                continue
            if p.expires_at is not None and now >= _aware(p.expires_at):
                continue
            if not _scope_matches(p.scope, scope):
                continue
            matched.append(p)
        if not matched:
            return None
        if len(matched) == 1:
            return matched[0]
        # Highest priority wins. Tie → policy_conflict.
        matched.sort(key=lambda e: e.priority, reverse=True)
        top = matched[0]
        ties = [p for p in matched if p.priority == top.priority]
        if len(ties) > 1:
            msg = (
                f"policy_conflict for kind={kind!r}: {len(ties)} active policies "
                f"share priority {top.priority}"
            )
            raise PolicyConflictError(msg)
        return top


def _scope_matches(policy_scope: dict[str, Any], request_scope: dict[str, Any]) -> bool:
    """Empty policy scope matches anything; non-empty policy scope must match.

    Slice 4 keeps scope matching simple: every key in policy_scope must
    appear in request_scope and either equal it OR — when the policy value
    is a list — contain the request value. Future slices may extend this.
    """
    if not policy_scope:
        return True
    for key, expected in policy_scope.items():
        actual = request_scope.get(key)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
