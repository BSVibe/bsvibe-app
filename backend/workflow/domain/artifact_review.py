"""The independent review of a verified artifact — pure: gather the context, parse the answer.

Verification judges one ledger item at a time and the counterfactual check sees only the cited
tests; nobody reads the WHOLE artifact asking "what breaks if the founder approves this?". This
module builds what that reviewer is shown (deterministically — code decides what goes in, within
a budget, and marks what was cut) and parses what it answers. The LLM call and the storage live
in ``application/artifact_review.py``.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import tokenize
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SEVERITIES = ("high", "medium", "low")
NO_PRODUCTION_CALL = "운영 호출 없음"
UNCONFIRMED = "확인 불가"
UNCONFIRMED_NAME = "(새 심볼)"
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
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
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
    is_method: bool = False
    unconfirmed: bool = False  # the file's new definitions could not be confirmed


@dataclass
class _FileSide:
    """The post-change side of one file as the diff shows it."""

    lines: dict[int, str]
    added: set[int]


def _parse_diff(diff: str) -> tuple[dict[str, _FileSide], set[tuple[str, str]]]:
    """Per file: new-side lines (by new line number) and the added line numbers; plus the
    ``(file, name)`` definitions the diff removes."""
    sides: dict[str, _FileSide] = {}
    removed: set[tuple[str, str]] = set()
    path = ""
    n = 1
    old_left = new_left = 0
    for line in diff.splitlines():
        in_hunk = old_left > 0 or new_left > 0
        if not in_hunk:
            if line.startswith("+++ "):
                path = line[4:].removeprefix("b/").strip()
                n = 1
                continue
            if line.startswith("@@"):
                m = _HUNK.match(line)
                if m:
                    old_left = int(m.group(1) or 1) if m.group(1) != "0" else 0
                    n = int(m.group(2))
                    new_left = int(m.group(3) or 1) if m.group(3) != "0" else 0
                continue
            if line.startswith("--- "):
                continue
        if not path or path == "/dev/null" or not line or line[0] not in "+- ":
            continue
        side = sides.setdefault(path, _FileSide({}, set()))
        body = line[1:]
        if line[0] == "-":
            old_left = max(old_left - 1, 0)
            match = _PY_DEF.match(body) or _TS_DEF.match(body)
            if match:
                removed.add((path, match.group(1)))
            continue
        if line[0] == "+":
            side.added.add(n)
            new_left = max(new_left - 1, 0)
        else:
            old_left = max(old_left - 1, 0)
            new_left = max(new_left - 1, 0)
        side.lines[n] = body
        n += 1
    return sides, removed


def _py_symbols(tree: ast.AST) -> list[tuple[int, str, bool]]:
    """``(line, name, is_method)`` of module-level and class-body definitions and assignments.

    Whether something is a method depends only on the enclosing block being a ``class``;
    ``if``/``try``/``with`` blocks are looked through, function bodies are not entered.
    """
    out: list[tuple[int, str, bool]] = []

    def walk(body: Sequence[ast.stmt], in_class: bool) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                out.append((node.lineno, node.name, in_class))
            elif isinstance(node, ast.ClassDef):
                out.append((node.lineno, node.name, in_class))
                walk(node.body, True)
            elif isinstance(node, ast.Assign):
                out.extend(
                    (node.lineno, t.id, in_class) for t in node.targets if isinstance(t, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign):
                if node.value is not None and isinstance(node.target, ast.Name):
                    out.append((node.lineno, node.target.id, in_class))
            else:
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(node, field, None)
                    if isinstance(sub, list):
                        walk(sub, in_class)
                for sub in (*getattr(node, "handlers", ()), *getattr(node, "cases", ())):
                    walk(sub.body, in_class)

    walk(getattr(tree, "body", []), False)
    return out


def _matches(side: _FileSide, source: str) -> bool:
    """The file text really is the post-change file: every added line sits where the diff says."""
    rows = source.split("\n")
    return all(
        n <= len(rows) and rows[n - 1].rstrip() == side.lines[n].rstrip() for n in side.added
    )


def _defined_here(path: str, side: _FileSide, source: str | None) -> list[tuple[str, bool]] | None:
    """Names defined on lines this diff added, in file order, with their method flag.

    ``None`` when the definitions cannot be CONFIRMED: the post-change file was not read (or is
    not the file the diff describes), or it does not parse. A hunk's partial text is never
    guessed at with a per-line regex — that is what mistook ``class or constant`` in a wrapped
    docstring for a class.
    """
    if not source or not _matches(side, source):
        return None
    if path.endswith(".py"):
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return None
        return [
            (name, is_method)
            for lineno, name, is_method in sorted(_py_symbols(tree))
            if lineno in side.added
        ]
    masked = masked_lines(path, source)
    if masked is None:
        return None
    out: list[tuple[str, bool]] = []
    for lineno in sorted(side.added):
        if lineno > len(masked):
            continue
        match = _TS_DEF.match(masked[lineno - 1])
        if match is not None:
            out.append((match.group(1), False))
    return out


def changed_source_paths(diff: str) -> list[str]:
    """Non-test files the diff adds lines to — the ones whose post-change text is worth reading."""
    sides, _ = _parse_diff(diff)
    return [p for p, side in sides.items() if side.added and not is_test_path(p)]


def new_public_symbols(diff: str, files: Mapping[str, str] | None = None) -> list[NewSymbol]:
    """Public functions / classes / methods / constants the diff ADDS, outside the test files.

    Definitions come from parsing the file AFTER the change (``files``: path -> text), so a
    docstring line that merely looks like ``class or ...`` is never a symbol. A file whose
    post-change text is missing or unparseable yields one ``unconfirmed`` placeholder symbol
    (reported as "확인 불가") instead of a guess. A name also removed in the same file is a
    move or an edit, not a new symbol.
    """
    sides, removed = _parse_diff(diff)
    seen: set[tuple[str, str]] = set()
    out: list[NewSymbol] = []
    for path, side in sides.items():
        if is_test_path(path):
            continue
        defined = _defined_here(path, side, (files or {}).get(path))
        if defined is None:
            if side.added:
                out.append(NewSymbol(UNCONFIRMED_NAME, path, unconfirmed=True))
            continue
        for name, is_method in defined:
            key = (path, name)
            if name.startswith("_") or key in removed or key in seen:
                continue
            seen.add(key)
            out.append(NewSymbol(name, path, is_method))
    return out


def _blank(text: str, spans: Sequence[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            chars[i] = " "
    return "".join(chars)


def _scan_code(line: str) -> str:
    """Generic (TS/JS-ish) mask: ``//`` comments and quoted strings become blanks."""
    stripped = line.lstrip()
    if stripped.startswith(("*", "/*")):
        return ""
    spans: list[tuple[int, int]] = []
    quote = ""
    start = 0
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                spans.append((start, i + 1))
                quote = ""
        elif ch in "'\"`":
            quote, start = ch, i
        elif line.startswith("//", i):
            spans.append((i, len(line)))
            break
        i += 1
    if quote:  # unterminated on this line (multi-line template literal): rest is string
        spans.append((start, len(line)))
    return _blank(line, spans)


def _python_code(line: str) -> str:
    """Mask comments and string literals of one Python line using ``tokenize``."""
    text = line.strip()
    skipped = {tokenize.COMMENT, tokenize.STRING}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        if hasattr(tokenize, name):
            skipped.add(getattr(tokenize, name))
    spans: list[tuple[int, int]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in skipped and tok.start[0] == tok.end[0] == 1:
                spans.append((tok.start[1], tok.end[1]))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return _scan_code_py(text)
    return _blank(text, spans)


def _scan_code_py(text: str) -> str:
    """Fallback when a single line is not tokenizable alone (e.g. inside a docstring)."""
    return _scan_code(text.split("#", 1)[0] if "#" in text and not _has_quote(text) else text)


def _has_quote(text: str) -> bool:
    return "'" in text or '"' in text


def code_part(path: str, text: str) -> str:
    """``text`` with comments and string-literal contents blanked out."""
    if path.endswith(".py"):
        return _python_code(text)
    return _scan_code(text)


def _blank_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """Blank ``spans`` (absolute offsets) but keep newlines so line numbers survive."""
    chars = list(text)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def _python_masked(text: str) -> str | None:
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    skipped = {tokenize.COMMENT, tokenize.STRING}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        if hasattr(tokenize, name):
            skipped.add(getattr(tokenize, name))
    spans: list[tuple[int, int]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in skipped:
                begin = starts[tok.start[0] - 1] + tok.start[1]
                end = starts[tok.end[0] - 1] + tok.end[1]
                spans.append((begin, end))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return _blank_spans(text, spans)


def _generic_masked(text: str) -> str:
    """TS/JS-ish: ``//`` and ``/* */`` comments, quoted strings and multi-line template
    literals become blanks."""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            spans.append((i, end))
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            spans.append((i, end))
            i = end
        elif ch in "'\"`":
            j = i + 1
            while j < n and text[j] != ch and (ch == "`" or text[j] != "\n"):
                j += 2 if text[j] == "\\" else 1
            spans.append((i, min(j + 1, n)))
            i = j + 1
        else:
            i += 1
    return _blank_spans(text, spans)


def masked_lines(path: str, text: str) -> list[str] | None:
    """The whole file with comments and string literals (multi-line ones too) blanked,
    one entry per line; ``None`` when it cannot be tokenized."""
    masked = _python_masked(text) if path.endswith(".py") else _generic_masked(text)
    return None if masked is None else masked.split("\n")


METHOD_UNCERTAIN = "이름만 같은 다른 클래스의 호출일 수 있음"


def production_calls(
    symbol: NewSymbol,
    grep_output: str,
    masked: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """``path:line:text`` hits of ``git grep -n`` that really call ``symbol`` in production code.

    Comments and string literals (so ``__all__`` entries too) are blanked first. A function,
    class or constant then counts on any occurrence of the name that is not its definition line
    and not preceded by ``.`` — a call, an argument passed without parentheses
    (``factory=build_x``), or a line of a multi-line import. A method counts as ``.name(`` or a
    bare ``.name`` reference (a bound method used as a callback).

    ``masked`` maps a path to its whole-file masked lines (``masked_lines``): a hit on such a
    file is judged against the file, so a line in the middle of a multi-line docstring is
    string, not code. Other files fall back to judging the single grep line.
    """
    hits: list[str] = []
    name = re.escape(symbol.name)
    definition = re.compile(rf"\b(?:def|class|function)\s+{name}\b")
    if symbol.is_method:
        use = re.compile(rf"\.\s*{name}(?![\w$])")
    else:
        use = re.compile(rf"(?<![\w$.]){name}(?![\w$])")
    for raw in grep_output.splitlines():
        path, _, rest = raw.partition(":")
        if not rest or is_test_path(path):
            continue
        # ``git grep -n`` prefixes the line number: path:LINE:text
        lineno, _, text = rest.partition(":")
        whole = (masked or {}).get(path)
        if whole is not None and lineno.isdigit() and 1 <= int(lineno) <= len(whole):
            code = whole[int(lineno) - 1]
        else:
            code = code_part(path, text)
        if definition.search(code):
            continue
        if use.search(code):
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
        if sym.unconfirmed:
            lines.append(
                f"- {sym.file}: 새 심볼 {UNCONFIRMED} (변경 후 파일을 읽거나 분석하지 못함)"
            )
        elif hits is None:
            lines.append(f"- {sym.name} ({sym.file}): {LOOKUP_FAILED}")
        elif not hits:
            lines.append(f"- {sym.name} ({sym.file}): {NO_PRODUCTION_CALL}")
        else:
            shown = hits[:MAX_CALLS_PER_SYMBOL]
            more = f" (+{len(hits) - len(shown)}곳 더)" if len(hits) > len(shown) else ""
            note = f" [{METHOD_UNCERTAIN}]" if sym.is_method else ""
            lines.append(f"- {sym.name} ({sym.file}): " + " | ".join(shown) + more + note)
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
    files_unmasked: Sequence[str] = (),
) -> list[dict[str, str]]:
    """A fresh conversation: the review system prompt and one user message of gathered facts."""
    calls_text = _calls_section(calls)
    if symbols_cut:
        calls_text += (
            f"\n(새 심볼 {symbols_cut}개는 예산 때문에 호출 위치를 조회하지 않았다.){TRUNCATED}"
        )
    if files_unmasked:
        calls_text += (
            f"\nfiles_unmasked ({len(files_unmasked)}개): {', '.join(files_unmasked)}"
            " — 읽기·구문 분석 실패 또는 파일 수 상한 때문에 한 줄 단위로만 주석·문자열을"
            " 판정했다. 독스트링 안의 이름이 호출로 잡혔을 수 있다."
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
