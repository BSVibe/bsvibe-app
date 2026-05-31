"""Runtime dataclasses for the rule engine.

ORM rows live in :mod:`backend.router.rules.db`. These dataclasses are
what the engine + conditions evaluate against — they decouple the
evaluation surface from the SQL schema.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Rough conversion factor — Latin words to GPT-like tokens.
_WORDS_TO_TOKENS_RATIO = 1.3

# CJK ranges (Hangul / Kana / CJK ideographs) + Latin for crude language
# detection. Only used by ``EvaluationContext.from_request``.
_CJK_RE = re.compile("[　-鿿가-힯぀-ゟ゠-ヿ]")
_HANGUL_RE = re.compile("[가-힯ㄱ-ㅣ]")
_KANA_RE = re.compile("[぀-ゟ゠-ヿ]")
_CJK_IDEO_RE = re.compile("[一-鿿]")
_LATIN_RE = re.compile(r"[a-zA-Z]")


def _detect_language(text: str) -> str | None:  # noqa: PLR0911 — script dispatch
    if not text:
        return None
    hangul = len(_HANGUL_RE.findall(text))
    kana = len(_KANA_RE.findall(text))
    cjk_ideo = len(_CJK_IDEO_RE.findall(text))
    total_cjk = hangul + kana + cjk_ideo
    if total_cjk == 0:
        latin = len(_LATIN_RE.findall(text))
        if latin / max(len(text), 1) > 0.3:
            return "en"
        return None
    if hangul > kana and hangul > cjk_ideo:
        return "ko"
    if kana > hangul:
        return "ja"
    if cjk_ideo > 0:
        return "zh"
    return None


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk_chars = len(_CJK_RE.findall(text))
    non_cjk = _CJK_RE.sub("", text)
    word_tokens = len(non_cjk.split())
    return int((word_tokens + cjk_chars) * _WORDS_TO_TOKENS_RATIO)


def _extract_user_text(messages: list[dict[str, Any]]) -> str:
    """Join all user-role message contents (last-message-first not required —
    rules can match against the whole user thread)."""
    return "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")


def _extract_system_prompt(data: dict[str, Any]) -> str:
    for m in data.get("messages", []):
        if m.get("role") == "system":
            return str(m.get("content", ""))
    return ""


def _extract_all_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(m.get("content", "")) for m in messages)


@dataclass
class RuleCondition:
    """One AND-clause inside a routing rule."""

    condition_type: str
    field: str
    operator: str
    value: Any
    negate: bool = False


@dataclass
class RoutingRule:
    """A routing rule scoped to ``(workspace_id, account_id)``."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    name: str
    priority: int
    is_active: bool
    is_default: bool
    target_model: str
    conditions: list[RuleCondition] = field(default_factory=list)


@dataclass
class RuleMatch:
    """Engine output: the winning rule + its target model + an audit trace."""

    rule: RoutingRule
    target_model: str
    trace: list[dict[str, Any]] | None = None


@dataclass
class EvaluationContext:
    """Pre-extracted request features evaluated once per request.

    Carries ``workspace_id`` + ``account_id`` so downstream condition
    evaluators (especially budget / request-count) can scope their reads.
    """

    workspace_id: uuid.UUID
    account_id: uuid.UUID
    user_text: str
    system_prompt: str
    all_text: str
    estimated_tokens: int
    conversation_turns: int
    has_code_blocks: bool
    has_error_trace: bool
    tool_count: int
    tool_names: list[str]
    original_model: str
    classified_intent: str | None = None
    detected_language: str | None = None
    hour_of_day: int | None = None
    day_of_week: str | None = None
    daily_cost: float | None = None
    monthly_cost: float | None = None
    request_count_hourly: int | None = None

    @classmethod
    def from_request(
        cls,
        data: dict[str, Any],
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        now: datetime | None = None,
    ) -> EvaluationContext:
        messages = data.get("messages", [])
        user_text = _extract_user_text(messages)
        all_text = _extract_all_text(messages)
        system_prompt = _extract_system_prompt(data)

        tools = data.get("tools", [])
        tool_names = [
            t.get("function", {}).get("name", "")
            for t in tools
            if t.get("function", {}).get("name")
        ]

        moment = now or datetime.now(UTC)
        day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

        return cls(
            workspace_id=workspace_id,
            account_id=account_id,
            user_text=user_text,
            system_prompt=system_prompt,
            all_text=all_text,
            estimated_tokens=_estimate_tokens(all_text),
            conversation_turns=len([m for m in messages if m.get("role") == "user"]),
            has_code_blocks=bool(re.search(r"```", all_text)),
            has_error_trace=any(p in all_text for p in ("Traceback", "Error:", "Exception")),
            tool_count=len(tools),
            tool_names=tool_names,
            original_model=data.get("model", ""),
            detected_language=_detect_language(user_text),
            hour_of_day=moment.hour,
            day_of_week=day_names[moment.weekday()],
        )
