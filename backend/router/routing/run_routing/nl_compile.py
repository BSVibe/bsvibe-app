"""Natural-language → run-routing rules compiler v2 (NL-native routing Lift N3).

The founder describes routing in plain language and one cheap LLM call compiles
it into structured, VALIDATED run-routing rule PROPOSALS. This is a DRY-RUN: it
never persists — the caller previews the proposals and applies them through the
apply endpoint (``POST /api/v1/run-routing/compile/apply``).

**Multi-dimension (founder constraint 2026-07-12):** routing is NOT hardcoded to
categories. A plain-language clause can be about different DIMENSIONS, and the
compiler detects which and emits the matching condition:

===============  ===========================================================
clause is about  field / mechanism
===============  ===========================================================
category/domain  ``classified_intent == <name>`` AND an intent definition
                 (``intent_name`` + a few ``intent_examples``) so the N1
                 classifier has something to match
complexity       ``estimated_tokens`` gt/lt OR ``pipeline == design_then_impl``
language         ``detected_language`` eq ko/en/ja/zh
artifact         ``artifact_type_hint`` eq code/pr/page/page_image
execution stage  ``caller_id`` == a known caller
"the rest"       ``is_default = True``
===============  ===========================================================

Every field is validated against the engine's ``ALLOWED_FIELDS`` /
``VALID_OPERATORS`` and the workspace's active accounts (the target catalog), so
a hallucinated field / operator / caller / target is DROPPED rather than trusted.
The LLM is INJECTED behind a Protocol (mocked in tests).

**Two failure modes, deliberately distinct** — collapsing them is what let an
infrastructure outage masquerade as the founder's bad phrasing for the entire
life of this feature:

* **We never reached the model** — ``llm.complete_text`` RAISED
  (``ExecutorAdapterUnavailable`` because no Redis was wired into the resolver, a
  dispatch timeout, a provider 5xx). Nothing about the founder's words is known
  to be wrong. → :class:`CompileLlmUnavailable`, which the REST / MCP callers
  surface as a 502 "couldn't reach the routing model".
* **The model answered, but nothing compiles** — unparseable output, or every
  proposal dropped by validation. → the historical degrade (``[]`` /
  :class:`UninterpretableCondition`), which the callers surface as a 422
  "couldn't interpret — try rephrasing".
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog

from backend.router.routing.run_routing.engine import ALLOWED_FIELDS, VALID_OPERATORS

logger = structlog.get_logger(__name__)


@runtime_checkable
class RoutingCompileLlm(Protocol):
    """The single cheap-LLM seam the compiler depends on. Production resolves a
    per-workspace gateway adapter; tests inject a stub returning canned JSON."""

    async def complete_text(self, *, system: str, user: str) -> str: ...


class CompileLlmUnavailable(Exception):
    """The compile model could not be REACHED — an INFRASTRUCTURE failure.

    Raised when ``llm.complete_text`` itself raises: the executor adapter has no
    Redis transport for the worker stream
    (:class:`~backend.dispatch.adapter.ExecutorAdapterUnavailable`), the dispatch
    times out, the provider 5xxs. We never got an ANSWER, so we know nothing
    about whether the founder's phrase was interpretable.

    This is emphatically NOT :class:`UninterpretableCondition` / an empty
    proposal list. Callers map it to a 502 "couldn't reach the routing model —
    try again", never to the 422 "try rephrasing" hint. The original production
    bug (a resolver built with no ``redis=``) was invisible for exactly this
    reason: the compiler swallowed the dispatch error and the founder was told
    their perfectly good Korean phrase was uninterpretable.
    """


@dataclass(frozen=True, slots=True)
class CompiledProposal:
    """One validated rule proposal.

    A proposal is exactly one of these shapes (mutually exclusive by validation):

    * **caller** — ``caller_id`` set, ``condition`` / ``intent_*`` None.
    * **condition** — ``condition`` set (a non-category dimension), the rest None.
    * **category** — ``condition`` keyed on ``classified_intent`` PLUS
      ``intent_name`` + ``intent_examples`` (the intent def to create on apply).
    * **default** — ``is_default`` True, ``caller_id`` / ``condition`` None.
    """

    name: str
    caller_id: str | None
    target: str
    priority: int
    is_default: bool
    condition: dict[str, Any] | None = None
    intent_name: str | None = None
    intent_examples: list[str] | None = None


# Keep the proposal count bounded so a runaway model can't flood the preview.
_MAX_RULES = 25
# Seed-example bounds for a category intent definition (design: 3-6).
_MIN_INTENT_EXAMPLES = 1
_MAX_INTENT_EXAMPLES = 12
_MAX_INTENT_NAME = 120

_SYSTEM_PROMPT = (
    "You compile a founder's plain-language routing description into run-routing "
    "rules for an autonomous engineering system. Each rule sends work matching "
    "some DIMENSION to a target MODEL. Detect which dimension each clause is about "
    "— do NOT force everything into categories. Respond with ONE JSON array (no "
    "prose, no code fences) of objects with these keys:\n"
    '  "name": a short human label for the rule,\n'
    '  "target": the EXACT model id from the "Models" catalog to route to,\n'
    '  "is_default": true ONLY for the single catch-all rule ("the rest" / '
    "'나머지' / '기본' / 'everything else'); false otherwise,\n"
    "  plus EXACTLY ONE of the following dimension keys (omit for the default):\n"
    '  - "caller_id": a caller id from the "Callers" catalog — use for EXECUTION '
    "STAGE clauses ('design'/'설계' → the plan caller, 'implement'/'구현' → the act "
    "caller, 'verify'/'검증' → the judge caller);\n"
    '  - "condition": {"field", "operator", "value"} for a non-category dimension:\n'
    "      * COMPLEXITY ('복잡한'/'큰 작업'/'간단한') → "
    'field "estimated_tokens" (operator gt/lt, an integer value) OR '
    'field "pipeline" (operator eq, value "design_then_impl" for complex);\n'
    "      * LANGUAGE ('한국어'/'영어') → "
    'field "detected_language" (operator eq, value one of ko/en/ja/zh);\n'
    "      * ARTIFACT ('코드'/'PR'/'페이지') → "
    'field "artifact_type_hint" (operator eq, value one of code/pr/page/page_image);\n'
    '  - "condition" keyed on field "classified_intent" (operator eq, value = the '
    'intent name) PLUS "intent_name" (a short snake_case id) PLUS "intent_examples" '
    "(3-6 short example phrases that belong to this category) — use for a "
    "DOMAIN/CATEGORY clause ('마케팅'/'디자인'/'문서'/'marketing'/'design').\n"
    '  Optionally "priority": an integer (lower runs first; use 10 for specific rules).\n'
    "Rules:\n"
    "- Use ONLY caller ids and model ids that appear verbatim in the catalogs. "
    "Drop anything you cannot map.\n"
    "- A category rule MUST include intent_name AND at least a few intent_examples.\n"
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


def _coerce_priority(raw_priority: Any) -> int:
    try:
        priority = int(raw_priority)
    except (TypeError, ValueError):
        priority = 10
    return priority if priority >= 1 else 10


def _coerce_condition(raw: Any) -> dict[str, Any] | None:
    """Validate a raw ``condition`` object against the engine whitelist, or drop.

    Returns the normalized ``{field, operator, value}`` dict when the field is in
    ``ALLOWED_FIELDS`` and the operator in ``VALID_OPERATORS`` (defaulting the
    operator to ``eq``), else ``None``."""
    if not isinstance(raw, dict):
        return None
    field = raw.get("field")
    if not isinstance(field, str) or field not in ALLOWED_FIELDS:
        return None
    operator = raw.get("operator", "eq")
    if not isinstance(operator, str) or operator not in VALID_OPERATORS:
        return None
    return {"field": field, "operator": operator, "value": raw.get("value")}


def _coerce_intent_examples(raw: Any) -> list[str] | None:
    """Return the cleaned list of seed example phrases, or ``None`` when there
    are none (a category with no examples can never classify)."""
    if not isinstance(raw, list):
        return None
    cleaned = [e.strip() for e in raw if isinstance(e, str) and e.strip()]
    if len(cleaned) < _MIN_INTENT_EXAMPLES:
        return None
    return cleaned[:_MAX_INTENT_EXAMPLES]


def _coerce_category(
    item: dict[str, Any],
    *,
    name: str,
    target: str,
    priority: int,
) -> CompiledProposal | None:
    """Coerce a DOMAIN/CATEGORY proposal (``intent_name`` present).

    Requires a valid ``intent_name`` + at least a few ``intent_examples``. The
    condition is forced to ``classified_intent == intent_name`` so the classifier
    label and the rule always agree (any model-supplied condition is ignored)."""
    intent_name_raw = item.get("intent_name")
    if not isinstance(intent_name_raw, str) or not intent_name_raw.strip():
        return None
    intent_name = intent_name_raw.strip()[:_MAX_INTENT_NAME]
    examples = _coerce_intent_examples(item.get("intent_examples"))
    if examples is None:
        return None
    return CompiledProposal(
        name=name,
        caller_id=None,
        target=target,
        priority=priority,
        is_default=False,
        condition={"field": "classified_intent", "operator": "eq", "value": intent_name},
        intent_name=intent_name,
        intent_examples=examples,
    )


def _coerce_proposal(  # noqa: PLR0911 — one return per dimension shape reads clearest
    item: Any,
    *,
    known_callers: set[str],
    known_targets: set[str],
) -> CompiledProposal | None:
    """Validate one raw object into a :class:`CompiledProposal`, or drop it.

    Every field is checked against the registry / the engine whitelist / the
    workspace's accounts — a hallucinated caller, field, operator, or target is
    dropped, never trusted. Dispatches on the dimension the object declares."""
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    target = item.get("target")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(target, str) or target not in known_targets:
        return None
    clean_name = name.strip()[:120]
    priority = _coerce_priority(item.get("priority", 10))

    # (1) Default catch-all — no dimension keys.
    if bool(item.get("is_default")):
        return CompiledProposal(
            name=clean_name,
            caller_id=None,
            target=target,
            priority=priority,
            is_default=True,
        )

    # (2) Category — an intent_name signals the domain dimension.
    if item.get("intent_name") is not None:
        return _coerce_category(item, name=clean_name, target=target, priority=priority)

    # (3) Caller (execution stage).
    caller_id = item.get("caller_id")
    if isinstance(caller_id, str) and caller_id:
        if caller_id not in known_callers:
            return None
        return CompiledProposal(
            name=clean_name,
            caller_id=caller_id,
            target=target,
            priority=priority,
            is_default=False,
        )

    # (4) Condition (complexity / language / artifact / etc.).
    condition = _coerce_condition(item.get("condition"))
    if condition is not None:
        return CompiledProposal(
            name=clean_name,
            caller_id=None,
            target=target,
            priority=priority,
            is_default=False,
            condition=condition,
        )

    # A non-default proposal with no usable dimension can never match — drop.
    return None


async def compile_rules(
    text: str,
    *,
    callers: list[tuple[str, str]],
    targets: list[tuple[str, str]],
    llm: RoutingCompileLlm,
) -> list[CompiledProposal]:
    """Compile ``text`` into validated proposals (dry-run, never persists).

    ``callers`` is ``[(caller_id, description)]`` and ``targets`` is
    ``[(account_label, litellm_model)]`` — the catalogs the LLM maps against.

    Returns ``[]`` on an empty description, unparseable output, or when nothing
    validates — i.e. the model ANSWERED and the answer was unusable (the caller
    surfaces "couldn't derive rules"). Raises :class:`CompileLlmUnavailable` when
    the model could not be reached at all — an infrastructure failure the caller
    must NOT report as the founder's bad wording."""
    if not text or not text.strip():
        return []
    known_callers = {c for c, _ in callers}
    known_targets = {t for _, t in targets}
    if not known_targets:
        return []

    prompt = _build_user_prompt(text, callers, targets)
    try:
        raw = await llm.complete_text(system=_SYSTEM_PROMPT, user=prompt)
    except Exception as exc:
        # We never reached the model. Degrading to [] here would report an
        # outage as "couldn't derive rules" — the exact masquerade this lift
        # removes. Surface it as infrastructure.
        logger.warning("routing_compile_llm_failed", exc_info=True)
        raise CompileLlmUnavailable(str(exc)) from exc

    items = _parse_json_array(raw)
    if items is None:
        logger.warning("routing_compile_unparseable")
        return []

    out: list[CompiledProposal] = []
    seen_default = False
    for item in items[:_MAX_RULES]:
        proposal = _coerce_proposal(item, known_callers=known_callers, known_targets=known_targets)
        if proposal is None:
            continue
        if proposal.is_default:
            if seen_default:
                continue  # only one catch-all default
            seen_default = True
        out.append(proposal)
    return out


# ---------------------------------------------------------------------------
# Single-condition compiler (NL-native routing Lift N5).
#
# The founder authors ONE rule = a free-text CONDITION phrase + a target model.
# The phrase compiles — transparently, per single rule — into ONE structured
# dimension: a caller_id (execution stage), a condition (complexity / language /
# artifact), or a category (classified_intent + an intent def to create). Unlike
# the paragraph compiler, there is no default / catch-all here (that's the
# workspace default), no target in the LLM's job (the founder picks it), and a
# nothing-valid result is an explicit UNINTERPRETABLE signal so the endpoint can
# 422 rather than persist a dead rule.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompiledCondition:
    """One validated dimension compiled from a single NL condition phrase.

    Exactly one dimension is expressed:

    * **caller** — ``caller_id`` set, ``condition`` / ``intent_*`` None.
    * **condition** — ``condition`` set (complexity / language / artifact), rest None.
    * **category** — ``condition`` keyed on ``classified_intent`` PLUS
      ``intent_name`` + ``intent_examples`` (the intent def to create on save).
    """

    caller_id: str | None = None
    condition: dict[str, Any] | None = None
    intent_name: str | None = None
    intent_examples: list[str] | None = None


@dataclass(frozen=True, slots=True)
class UninterpretableCondition:
    """Signal that the phrase compiled to nothing valid — the endpoint 422s
    rather than persisting a rule that could never route."""


_SINGLE_SYSTEM_PROMPT = (
    "You compile ONE plain-language routing CONDITION (the founder's free text, "
    "e.g. '복잡한 작업' / '마케팅 관련' / '한국어 요청' / '설계 단계') into ONE "
    "structured routing dimension. Detect which DIMENSION the phrase is about — do "
    "NOT force everything into categories. Respond with ONE JSON object (no prose, "
    "no code fences) containing EXACTLY ONE of these dimension shapes:\n"
    '  - "caller_id": a caller id from the "Callers" catalog — for an EXECUTION '
    "STAGE phrase ('design'/'설계' → the plan caller, 'implement'/'구현' → the act "
    "caller, 'verify'/'검증' → the judge caller);\n"
    '  - "condition": {"field", "operator", "value"} for a non-category dimension:\n'
    "      * COMPLEXITY ('복잡한'/'큰 작업'/'간단한') → "
    'field "estimated_tokens" (operator gt/lt, an integer value) OR '
    'field "pipeline" (operator eq, value "design_then_impl" for complex);\n'
    "      * LANGUAGE ('한국어'/'영어') → "
    'field "detected_language" (operator eq, value one of ko/en/ja/zh);\n'
    "      * ARTIFACT ('코드'/'PR'/'페이지') → "
    'field "artifact_type_hint" (operator eq, value one of code/pr/page/page_image);\n'
    '  - "intent_name" (a short snake_case id) PLUS "intent_examples" (3-6 short '
    "example phrases that belong to this category) — for a DOMAIN/CATEGORY phrase "
    "('마케팅'/'디자인'/'문서'/'marketing'/'design'). The rule is keyed on "
    "classified_intent automatically.\n"
    "Rules:\n"
    "- Use ONLY caller ids that appear verbatim in the catalog. Emit nothing you "
    "cannot map.\n"
    "- A category MUST include intent_name AND at least a few intent_examples.\n"
    "- Do NOT emit a target model — the founder picks it separately."
)


def _build_single_user_prompt(text: str, callers: list[tuple[str, str]]) -> str:
    lines = [f"Condition:\n{text.strip() or '(empty)'}", "", "Callers:"]
    for caller_id, desc in callers:
        lines.append(f"- {caller_id}: {desc}")
    return "\n".join(lines)


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    """Parse the LLM's single JSON object, tolerating a code fence or an array
    wrapper (take the first object element)."""
    if not raw or not raw.strip():
        return None
    candidate = raw.strip()
    obj_start = candidate.find("{")
    arr_start = candidate.find("[")
    # An array wrapper only when '[' precedes the first '{' (else the '[' is a
    # nested intent_examples list inside the object).
    if arr_start != -1 and (obj_start == -1 or arr_start < obj_start):
        arr = _parse_json_array(candidate)
        if arr is not None:
            for item in arr:
                if isinstance(item, dict):
                    return item
        return None
    end = candidate.rfind("}")
    if obj_start == -1 or end == -1 or end < obj_start:
        return None
    try:
        data = json.loads(candidate[obj_start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _coerce_single_condition(  # noqa: PLR0911 — one return per dimension shape reads clearest
    item: dict[str, Any],
    *,
    known_callers: set[str],
) -> CompiledCondition | None:
    """Validate one raw object into a :class:`CompiledCondition`, or ``None``.

    Dispatches on the dimension the object declares — every field is checked
    against the caller registry / the engine whitelist. There is no default and
    no target here (the single-rule model owns those elsewhere)."""
    # (1) Category — an intent_name signals the domain dimension.
    intent_name_raw = item.get("intent_name")
    if intent_name_raw is not None:
        if not isinstance(intent_name_raw, str) or not intent_name_raw.strip():
            return None
        intent_name = intent_name_raw.strip()[:_MAX_INTENT_NAME]
        examples = _coerce_intent_examples(item.get("intent_examples"))
        if examples is None:
            return None
        return CompiledCondition(
            condition={"field": "classified_intent", "operator": "eq", "value": intent_name},
            intent_name=intent_name,
            intent_examples=examples,
        )

    # (2) Caller (execution stage).
    caller_id = item.get("caller_id")
    if isinstance(caller_id, str) and caller_id:
        if caller_id not in known_callers:
            return None
        return CompiledCondition(caller_id=caller_id)

    # (3) Condition (complexity / language / artifact / etc.).
    condition = _coerce_condition(item.get("condition"))
    if condition is not None:
        return CompiledCondition(condition=condition)

    return None


async def compile_source_text(
    text: str,
    *,
    callers: list[tuple[str, str]],
    llm: RoutingCompileLlm,
) -> CompiledCondition | UninterpretableCondition:
    """Compile ONE NL condition phrase into ONE validated dimension.

    ``callers`` is ``[(caller_id, description)]`` — the catalog the LLM maps stage
    phrases against. Returns a :class:`CompiledCondition` on success, or an
    :class:`UninterpretableCondition` on an empty phrase, unparseable output, or
    when nothing validates — i.e. the model ANSWERED and the answer was unusable.

    Raises :class:`CompileLlmUnavailable` when the model could not be REACHED. The
    live production symptom of collapsing the two was this log line, emitted on
    every single rule save, while the founder saw "try rephrasing"::

        event: routing_source_text_compile_llm_failed
        ExecutorAdapterUnavailable: ExecutorAdapter requires a Redis client …
    """
    if not text or not text.strip():
        return UninterpretableCondition()
    known_callers = {c for c, _ in callers}

    prompt = _build_single_user_prompt(text, callers)
    try:
        raw = await llm.complete_text(system=_SINGLE_SYSTEM_PROMPT, user=prompt)
    except Exception as exc:
        logger.warning("routing_source_text_compile_llm_failed", exc_info=True)
        raise CompileLlmUnavailable(str(exc)) from exc

    item = _parse_json_object(raw)
    if item is None:
        logger.warning("routing_source_text_unparseable")
        return UninterpretableCondition()

    compiled = _coerce_single_condition(item, known_callers=known_callers)
    if compiled is None:
        return UninterpretableCondition()
    return compiled


def as_dicts(proposals: Iterable[CompiledProposal]) -> list[dict[str, Any]]:
    """The apply-endpoint wire shape for each proposal (:class:`ApplyProposal` 1:1)."""
    return [
        {
            "name": p.name,
            "caller_id": p.caller_id,
            "target": p.target,
            "priority": p.priority,
            "is_default": p.is_default,
            "condition": p.condition,
            "intent_name": p.intent_name,
            "intent_examples": p.intent_examples,
        }
        for p in proposals
    ]


__all__ = [
    "CompileLlmUnavailable",
    "CompiledCondition",
    "CompiledProposal",
    "RoutingCompileLlm",
    "UninterpretableCondition",
    "as_dicts",
    "compile_rules",
    "compile_source_text",
]
