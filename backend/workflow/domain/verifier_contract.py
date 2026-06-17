"""Verification Contract — the work LLM's declared "how this is checked".

Design: ``~/Docs/BSNexus_Verification_Contract_Design_2026-05-17.md``.

The work LLM declares a contract before doing the work (via the
``declare_verification`` tool). The contract is a list of checks; each
check is either:

  - ``command`` — a shell command; exit 0 is the verdict. Deterministic.
  - ``judge``   — an LLM-as-judge rubric of concrete criteria. For
                  non-executable / non-dev deliverables.

The parser is deliberately tolerant of imperfect LLM JSON: it drops
invalid checks rather than rejecting the whole contract, and returns
``None`` only when nothing usable remains (→ ``human_review_required``,
never a silent pass).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

CheckKind = Literal["command", "judge"]
_VALID_KINDS: frozenset[str] = frozenset({"command", "judge"})


@dataclass(frozen=True)
class VerificationCheck:
    """One declared check. ``command`` is set for ``kind='command'``;
    ``criteria`` is set for ``kind='judge'``."""

    kind: CheckKind
    command: str | None = None
    criteria: tuple[str, ...] = ()
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "command":
            return {"kind": "command", "command": self.command, "rationale": self.rationale}
        return {"kind": "judge", "criteria": list(self.criteria), "rationale": self.rationale}


@dataclass(frozen=True)
class VerificationContract:
    """The full declared contract for one RunAttempt's work step."""

    checks: tuple[VerificationCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"checks": [c.to_dict() for c in self.checks]}

    @property
    def command_checks(self) -> tuple[VerificationCheck, ...]:
        return tuple(c for c in self.checks if c.kind == "command")

    @property
    def judge_checks(self) -> tuple[VerificationCheck, ...]:
        return tuple(c for c in self.checks if c.kind == "judge")


#: Lift E38 — natural-English aliases LLMs reach for when the prompt
#: template is paraphrased. ``shell`` reads as the prose ("shell command")
#: the E37 dogfood (session ``ses_12c8f0be2``, 2026-06-17) proved
#: qwen3.6-plus emits even when the template prescribes ``command``. The
#: parser normalizes both onto the canonical ``command`` kind.
_KIND_ALIASES: dict[str, str] = {"shell": "command", "command": "command", "judge": "judge"}


def _parse_check(raw: Any) -> VerificationCheck | None:
    """Normalize one raw check dict. Returns None when unusable.

    Lift E38 — tolerant of ``kind: "shell"`` (mapped to ``command``) and
    ``cmd`` (mapped to ``command``) so the agent's first emit shape lands
    instead of triggering a re-prompt loop. The canonical
    ``{"kind": "command", "command": "…"}`` continues to parse unchanged.
    """
    if not isinstance(raw, dict):
        return None
    raw_kind = str(raw.get("kind") or "").strip().lower()
    kind = _KIND_ALIASES.get(raw_kind)
    if kind is None:
        return None
    rationale = str(raw.get("rationale") or "").strip()
    if kind == "command":
        # E38 — accept ``command`` (canonical) OR ``cmd`` (alias the E37
        # prompt template used before E38 aligned it).
        command = str(raw.get("command") or raw.get("cmd") or "").strip()
        if not command:
            return None
        return VerificationCheck(kind="command", command=command, rationale=rationale)
    # judge
    raw_criteria = raw.get("criteria")
    if not isinstance(raw_criteria, list):
        return None
    criteria: list[str] = []
    for item in raw_criteria:
        text = str(item).strip()
        if text and text not in criteria:
            criteria.append(text)
    if not criteria:
        return None
    return VerificationCheck(kind="judge", criteria=tuple(criteria), rationale=rationale)


def parse_verification_contract(raw: Any) -> VerificationContract | None:
    """Parse an LLM-declared contract into a normalized
    :class:`VerificationContract`, or ``None`` when no usable check
    remains. Tolerant — invalid checks are dropped, not fatal."""
    if not isinstance(raw, dict):
        return None
    raw_checks = raw.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        return None
    checks = [c for c in (_parse_check(item) for item in raw_checks) if c is not None]
    if not checks:
        return None
    return VerificationContract(checks=tuple(checks))


__all__ = [
    "CheckKind",
    "VerificationCheck",
    "VerificationContract",
    "parse_verification_contract",
]
