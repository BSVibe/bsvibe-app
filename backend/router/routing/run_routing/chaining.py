"""종합 — 워크스페이스의 라우팅 룰에서 단계 어휘를 도출한다.

The founder's routing rules are the ONLY source of what work stages exist.
Before this module the frame stage predicted the shape of the work itself from
a fixed two-value vocabulary (``single`` / ``design_then_impl``) — a system
guess layered on top of the user's routing, which is the second-source shape
INV-7 keeps flagging.

prod measurement (2026-08-24) showed what that guess was worth: the workspace
where the founder actually works had **zero** routing rules, yet the framer
marked 32 runs ``design_then_impl`` and split each into two runs. All 21
recorded routing decisions resolved to ``workspace_default``. The split bought
nothing and cost a second run every time.

So the direction inverts. The rules are read first; whatever stage labels they
key on ARE the workspace's vocabulary; the frame stage then splits the request
into steps drawn from that vocabulary, and the existing rule engine assigns each
step a model exactly as it always has. **No rules → no vocabulary → no split**,
which is a fail-closed default: a system that was told nothing predicts nothing.

Deliberately NOT persisted. The vocabulary is a pure function of the rules in
force at frame time, so a rule the founder edits or disables takes effect on the
next run with nothing to invalidate — and there is no second copy to drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.router.routing.run_routing.db import RunRoutingRuleRow

#: The routing field whose values name the stages of a workspace's work.
#: Already in the engine's ``ALLOWED_FIELDS`` and already a free string — this
#: module adds no axis, it reads the one the founder was always writing.
STAGE_FIELD = "stage"

#: Condition operators that assert "the work IS in this stage". ``regex`` /
#: comparison operators describe a shape, not a nameable stage, so they
#: contribute no vocabulary — a stage the framer cannot name it cannot assign.
_NAMING_OPERATORS = frozenset({"eq", "in"})


@dataclass(frozen=True, slots=True)
class StageTerm:
    """One stage the founder's rules distinguish.

    ``label`` is the exact value a rule matches on (what a run's payload must
    carry for that rule to fire). ``description`` is what the framer reads to
    decide which work belongs here — the founder's own condition phrase
    (``source_text``) when the rule was authored in natural language, else the
    rule's name. Never a system-invented gloss: the point is that the founder's
    words, not ours, define the split.
    """

    label: str
    description: str


def _labels(condition: dict[str, Any]) -> list[str]:
    """The stage labels one condition names, or ``[]``."""
    if condition.get("field") != STAGE_FIELD:
        return []
    if condition.get("operator", "eq") not in _NAMING_OPERATORS:
        return []
    # ``stage != design`` says which stage the work is NOT in — it names no
    # stage the framer could assign a step to.
    if condition.get("negate"):
        return []
    value = condition.get("value")
    candidates = value if isinstance(value, list) else [value]
    return [c.strip() for c in candidates if isinstance(c, str) and c.strip()]


def derive_stage_vocabulary(rules: list[RunRoutingRuleRow]) -> list[StageTerm]:
    """The stages this workspace's ACTIVE rules distinguish.

    Empty when no rule keys on ``stage`` — including the common case of a
    workspace with no rules at all. The caller must treat empty as "do not
    split", never as "fall back to a built-in vocabulary": a built-in default
    is exactly the prediction this replaces.

    This is a SET of stages the work can be in, **not** the order to do them in
    — routing priority says which rule wins a match, which is unrelated to
    sequence. The framer is told as much, and picks the order from the request.
    The ordering here exists only so the same rules always produce the same
    prompt: prod's two stage rules share ``priority=10``, so without the
    name tiebreak the list would follow whatever order the DB happened to
    return and the model would see a different prompt run to run.
    """
    terms: dict[str, StageTerm] = {}
    for rule in sorted(rules, key=lambda r: (r.priority, r.name, str(r.id))):
        if not rule.is_active:
            continue
        conditions = rule.conditions if isinstance(rule.conditions, list) else []
        description = (rule.source_text or "").strip() or rule.name
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            for label in _labels(condition):
                terms.setdefault(label, StageTerm(label=label, description=description))
    return list(terms.values())


__all__ = ["STAGE_FIELD", "StageTerm", "derive_stage_vocabulary"]
