"""The independent review of a verified artifact — pure: gather the context, parse the answer.

Verification judges one ledger item at a time and the counterfactual check sees only the cited
tests; nobody reads the WHOLE artifact asking "what breaks if the founder approves this?". This
module builds what that reviewer is shown (deterministically — code decides what goes in, within
a budget, and marks what was cut) and parses what it answers. The LLM call and the storage live
in ``application/artifact_review.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SEVERITIES = ("high", "medium", "low")
NO_PRODUCTION_CALL = "운영 호출 없음"
LOOKUP_FAILED = "호출 위치 조회 실패"
TRUNCATED = "\n[... TRUNCATED ...] — 예산 초과로 여기서 잘렸다."

# Per-section character budgets: the whole context stays bounded however big the change is.
_REQUEST_CHARS = 4_000
_LEDGER_CHARS = 6_000
_DIFF_CHARS = 30_000
_CALLS_CHARS = 10_000
_GATE_CHARS = 5_000

MAX_SYMBOLS = 40
MAX_CALLS_PER_SYMBOL = 5

REVIEW_SYSTEM_PROMPT = (
    "너는 독립 리뷰어다. 이 산출물을 만든 에이전트와 대화한 적이 없고, 아래 맥락만 본다.\n"
    "창업자가 이 산출물을 거부할 이유를 찾아라. 각 이유는 구체적인 실패 시나리오"
    "(이런 입력·상태 → 이런 잘못된 결과)로 쓰고, 근거가 없으면 내지 마라.\n"
    f"'{NO_PRODUCTION_CALL}'으로 표시된 새 공개 함수·클래스는 운영 코드에서 아무도 부르지 않는다"
    " — 테스트만 초록일 수 있다. 테스트가 쓰는 입력 모양이 실제 운영 입력과 같은지도 본다."
    " 잘린 부분을 근거로 단정하지 마라.\n"
    '답은 JSON 객체 하나만: {"findings": [{"severity": "high|medium|low", "file": "경로",'
    ' "line": 줄 번호 또는 null, "claim": "무엇이 잘못인가",'
    ' "failure_scenario": "이런 입력·상태 → 이런 잘못된 결과"}]}.'
    ' 거부할 이유가 없으면 {"findings": []}.'
)

_TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs", "e2e"}
_TEST_FILE = re.compile(
    r"(^|/)(test_[^/]*\.py|[^/]*_test\.py|conftest\.py|[^/]*\.(test|spec)\.[jt]sx?)$"
)
_PY_DEF = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")
_TS_DEF = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)"
)


def is_test_path(path: str) -> bool:
    """A file that exists to test — calls from it are not production use."""
    parts = path.replace("\\", "/").split("/")
    return any(p in _TEST_DIRS for p in parts[:-1]) or bool(_TEST_FILE.search(path))


@dataclass(frozen=True)
class NewSymbol:
    name: str
    file: str


def new_public_symbols(diff: str) -> list[NewSymbol]:
    """Public functions / classes / methods the diff ADDS, outside the test files.

    A name that is also removed in the same file is a move or an edit, not a new symbol.
    """
    added: list[NewSymbol] = []
    removed: set[tuple[str, str]] = set()
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/").strip()
            continue
        if line.startswith("--- ") or not line or line[0] not in "+-":
            continue
        match = _PY_DEF.match(line[1:]) or _TS_DEF.match(line[1:])
        if match is None or not path or path == "/dev/null":
            continue
        name = match.group(1)
        if line[0] == "-":
            removed.add((path, name))
        elif not name.startswith("_") and not is_test_path(path):
            added.append(NewSymbol(name, path))
    seen: set[tuple[str, str]] = set()
    out: list[NewSymbol] = []
    for sym in added:
        key = (sym.file, sym.name)
        if key not in removed and key not in seen:
            seen.add(key)
            out.append(sym)
    return out


def production_calls(symbol: NewSymbol, grep_output: str) -> list[str]:
    """``path:line:text`` hits of ``git grep -n`` that are neither tests nor the definition."""
    hits: list[str] = []
    definition = re.compile(rf"\b(?:def|class|function)\s+{re.escape(symbol.name)}\b")
    for raw in grep_output.splitlines():
        path, _, rest = raw.partition(":")
        if not rest or is_test_path(path) or definition.search(rest):
            continue
        hits.append(raw.strip())
    return hits


def tree_fingerprint(diff: str) -> str:
    """Same diff against the same baseline → same tree → same fingerprint."""
    return hashlib.sha256(diff.encode("utf-8", errors="replace")).hexdigest()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + TRUNCATED


def _ledger_section(ledger: Sequence[Mapping[str, Any]]) -> str:
    lines = []
    for row in ledger:
        note = f" — {row['note']}" if row.get("note") else ""
        lines.append(
            f"- {row.get('id')} [{row.get('kind')}] {row.get('status')}: {row.get('text')}{note}"
        )
    return "\n".join(lines) or "(장부 없음)"


def _calls_section(calls: Mapping[NewSymbol, list[str] | None]) -> str:
    if not calls:
        return "(새 공개 함수·클래스·메서드 없음)"
    lines = []
    for sym, hits in calls.items():
        if hits is None:
            lines.append(f"- {sym.name} ({sym.file}): {LOOKUP_FAILED}")
        elif not hits:
            lines.append(f"- {sym.name} ({sym.file}): {NO_PRODUCTION_CALL}")
        else:
            shown = hits[:MAX_CALLS_PER_SYMBOL]
            more = f" (+{len(hits) - len(shown)}곳 더)" if len(hits) > len(shown) else ""
            lines.append(f"- {sym.name} ({sym.file}): " + " | ".join(shown) + more)
    return "\n".join(lines)


def _gate_section(commands: Sequence[Mapping[str, Any]]) -> str:
    lines = []
    for c in commands:
        status = c.get("status") or ("passed" if c.get("passed") else "failed")
        lines.append(
            f"- [{c.get('kind') or 'command'}] {c.get('command')} → {status}"
            f" (exit {c.get('exit_code')})"
        )
    return "\n".join(lines) or "(게이트 명령 없음)"


def build_review_messages(
    *,
    request: str,
    ledger: Sequence[Mapping[str, Any]],
    diff: str,
    calls: Mapping[NewSymbol, list[str] | None],
    gate_commands: Sequence[Mapping[str, Any]],
    symbols_cut: int = 0,
) -> list[dict[str, str]]:
    """A fresh conversation: the review system prompt and one user message of gathered facts."""
    calls_text = _calls_section(calls)
    if symbols_cut:
        calls_text += (
            f"\n(새 심볼 {symbols_cut}개는 예산 때문에 호출 위치를 조회하지 않았다.){TRUNCATED}"
        )
    user = "\n\n".join(
        [
            "## 요청 원문\n" + _clip(request or "(없음)", _REQUEST_CHARS),
            "## 장부 (항목 · 판정 · note)\n" + _clip(_ledger_section(ledger), _LEDGER_CHARS),
            "## 기준 대비 전체 diff\n" + _clip(diff or "(diff 없음)", _DIFF_CHARS),
            "## 새 공개 심볼의 운영 코드 호출 위치 (테스트 제외)\n"
            + _clip(calls_text, _CALLS_CHARS),
            "## 게이트가 돌린 명령과 결과\n" + _clip(_gate_section(gate_commands), _GATE_CHARS),
        ]
    )
    return [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _json_object(raw: str) -> Any:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except ValueError:
        return None


_FILE_MAX = 200


def _clean_file(value: Any) -> str | None:
    """A reviewer's file path, or ``None`` when it could break a one-line PR entry."""
    if not isinstance(value, str):
        return None
    path = value.strip()
    if not path or len(path) > _FILE_MAX or "`" in path:
        return None
    if any(not ch.isprintable() for ch in path):
        return None
    return path


def parse_review(raw: str) -> dict[str, Any] | None:
    """``{"status": "done", "findings": [...]}``, or ``None`` when the output is not a review."""
    data = _json_object(raw or "")
    items = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    findings: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        severity = str(item.get("severity") or "").strip().lower()
        claim = str(item.get("claim") or "").strip()
        scenario = str(item.get("failure_scenario") or "").strip()
        if severity not in SEVERITIES or not claim or not scenario:
            continue
        line = item.get("line")
        valid_line = isinstance(line, int) and not isinstance(line, bool) and line >= 1
        findings.append(
            {
                "severity": severity,
                "file": _clean_file(item.get("file")),
                "line": line if valid_line else None,
                "claim": claim,
                "failure_scenario": scenario,
            }
        )
    if items and not findings:
        return None
    return {"status": "done", "findings": findings}


def unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}
