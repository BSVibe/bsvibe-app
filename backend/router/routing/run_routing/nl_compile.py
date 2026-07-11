"""Natural-language → run-routing rules compiler (unified routing Lift 5).

The founder describes routing in plain language ("설계는 opus, 나머지는 sonnet" /
"send big-context work to opus, everything else to sonnet") and one cheap LLM
call compiles it into structured, VALIDATED run-routing rule proposals. This is a
DRY-RUN: it never persists — the caller previews the proposals and applies them
through the normal create endpoint.

Mirrors the frame stage's seam (:class:`backend.workflow.application.stages.frame`)
— a single ``(system, user) -> text`` LLM call behind a Protocol, tolerant JSON
parsing, and every field validated against the registry / the workspace's own
accounts so a hallucinated caller or target is dropped rather than trusted.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)


@runtime_checkable
class RoutingCompileLlm(Protocol):
    """The single cheap-LLM seam the compiler depends on. Production resolves a
    per-workspace gateway adapter; tests inject a stub returning canned JSON."""

    async def complete_text(self, *, system: str, user: str) -> str: ...


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """One validated rule proposal — the exact shape the create endpoint takes."""

    name: str
    caller_id: str | None
    target: str
    priority: int
    is_default: bool


# Keep the proposal count bounded so a runaway model can't flood the preview.
_MAX_RULES = 25

_SYSTEM_PROMPT = (
    "You compile a founder's plain-language routing description into run-routing "
    "rules for an autonomous engineering system. Each rule sends one dispatch "
    "CALLER's work to a target MODEL. Respond with ONE JSON array (no prose, no "
    "code fences) of objects with these keys:\n"
    '  "name": a short human label for the rule,\n'
    '  "caller_id": the EXACT caller id from the "Callers" catalog this rule '
    "routes, or null ONLY for the single catch-all default rule,\n"
    '  "target": the EXACT model id from the "Models" catalog to route to,\n'
    '  "priority": an integer (lower runs first; use 10 for specific rules),\n'
    '  "is_default": true for the single catch-all default rule (which sets '
    "caller_id to null), false otherwise.\n"
    "Rules:\n"
    "- Use ONLY caller ids and model ids that appear verbatim in the catalogs. "
    "Drop anything you cannot map.\n"
    "- Map plain-language stages to caller ids by meaning: 'design'/'plan'/'설계' "
    "→ the agent-loop plan caller; 'implement'/'act'/'구현' → the agent-loop act "
    "caller; 'verify'/'judge' → the judge caller.\n"
    "- 'the rest' / 'default' / 'everything else' / '나머지' / '기본' → ONE rule "
    "with is_default true and caller_id null.\n"
    "- Emit at most one default rule."
)


def _build_user_prompt(
    text: str,
    callers: list[tuple[str, str]],
    targets: list[tuple[str, str]],
) -> str:
    lines = [f"Description:\n{text.strip() or '(empty)'}", "", "Callers:"]
    for caller_id, desc in callers:
        lines.append(f"- {caller_id}: {desc}")
    lines.append("")
    lines.append("Models:")
    for label, litellm_model in targets:
        lines.append(f"- {litellm_model} ({label})")
    return "\n".join(lines)


def _parse_json_array(raw: str) -> list[Any] | None:
    """Parse the LLM's JSON array, tolerating a leading/trailing code fence."""
    if not raw or not raw.strip():
        return None
    candidate = raw.strip()
    start = candidate.find("[")
    end = candidate.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(candidate[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, list) else None


def _coerce_rule(
    item: Any,
    *,
    known_callers: set[str],
    known_targets: set[str],
) -> CompiledRule | None:
    """Validate one raw object into a :class:`CompiledRule`, or drop it.

    Every field is checked against the registry / the workspace's accounts —
    a hallucinated caller or target is dropped, never trusted."""
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    target = item.get("target")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(target, str) or target not in known_targets:
        return None

    is_default = bool(item.get("is_default"))
    caller_id = item.get("caller_id")
    if is_default:
        caller_id = None
    # Non-default rules MUST name a known caller (the resolver's column-first
    # matcher would otherwise never fire) — drop a rule that can't.
    elif not isinstance(caller_id, str) or caller_id not in known_callers:
        return None

    raw_priority = item.get("priority", 10)
    try:
        priority = int(raw_priority)
    except (TypeError, ValueError):
        priority = 10
    if priority < 1:
        priority = 10

    return CompiledRule(
        name=name.strip()[:120],
        caller_id=caller_id,
        target=target,
        priority=priority,
        is_default=is_default,
    )


async def compile_rules(
    text: str,
    *,
    callers: list[tuple[str, str]],
    targets: list[tuple[str, str]],
    llm: RoutingCompileLlm,
) -> list[CompiledRule]:
    """Compile ``text`` into validated rule proposals (dry-run, never persists).

    ``callers`` is ``[(caller_id, description)]`` and ``targets`` is
    ``[(account_label, litellm_model)]`` — the two catalogs the LLM maps against.
    Returns ``[]`` on an empty description, an LLM failure, unparseable output, or
    when nothing validates (the caller surfaces "couldn't derive rules")."""
    if not text or not text.strip():
        return []
    known_callers = {c for c, _ in callers}
    known_targets = {t for _, t in targets}
    if not known_targets:
        return []

    prompt = _build_user_prompt(text, callers, targets)
    try:
        raw = await llm.complete_text(system=_SYSTEM_PROMPT, user=prompt)
    except Exception:  # noqa: BLE001 — compile must never raise; it degrades to []
        logger.warning("routing_compile_llm_failed", exc_info=True)
        return []

    items = _parse_json_array(raw)
    if items is None:
        logger.warning("routing_compile_unparseable")
        return []

    out: list[CompiledRule] = []
    seen_default = False
    for item in items[:_MAX_RULES]:
        rule = _coerce_rule(item, known_callers=known_callers, known_targets=known_targets)
        if rule is None:
            continue
        if rule.is_default:
            if seen_default:
                continue  # only one catch-all default
            seen_default = True
        out.append(rule)
    return out


def as_dicts(rules: Iterable[CompiledRule]) -> list[dict[str, Any]]:
    """The create-endpoint wire shape for each proposal (RunRuleCreate 1:1)."""
    return [
        {
            "name": r.name,
            "caller_id": r.caller_id,
            "target": r.target,
            "priority": r.priority,
            "is_default": r.is_default,
        }
        for r in rules
    ]


__all__ = [
    "CompiledRule",
    "RoutingCompileLlm",
    "as_dicts",
    "compile_rules",
]
