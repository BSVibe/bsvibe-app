"""The requirements ledger — what a request asked for, item by item, and the evidence for each.

Before this, nothing checked a run against its request: the agent picked its own checks, the
judge's criteria came from knowledge retrieval (always advisory), and the scope check only
flagged files that should not have changed. A run could do one of five asked-for things and
pass (#1204, 2026-10-26).

**v1 is narrow on purpose.** A ledger exists only when the request STRUCTURES what it wants:
unticked checkboxes anywhere, or list items directly under a heading that names work to do,
guards, a done criterion or a constraint. Free prose has none — an agent cannot owe evidence
for "make it nicer", and a ledger it cannot satisfy only loops the run. Evidence is a path in
this run's diff, or a test whose file is in the diff or covered by a command that passed.
Nothing is grounded by matching words in the agent's own summary. Constraint items ("do not
…") are listed, not proved — no path can show that something was NOT done.

Pure: no I/O. The application layer (``requirements_ledger``) records, judges and reads.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Run payload key holding the ledger (written once, at framing).
REQUIREMENTS_KEY = "requirements"
#: ``VerificationResult.result`` key holding the verdict rows of that round.
LEDGER_RESULT_KEY = "requirements"

REQUIREMENT_KINDS = ("do", "guard", "done", "constraint")
#: Item statuses. ``pending`` = grounded, awaiting the item judge. ``noted`` = a constraint.
OWED_KINDS = frozenset({"do", "guard", "done"})

#: Comment authors whose words are instructions on a public repo.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

_ITEM_MAX_CHARS = 300
_MAX_ITEMS = 40

# Heading words → kind. Matched against the heading text with its marks and any trailing
# parenthesis stripped, lower-cased. A heading not listed here makes its list NOT items.
_HEADING_KINDS: tuple[tuple[str, str], ...] = (
    ("할 것", "do"),
    ("해야 할 것", "do"),
    ("원하는 동작", "do"),
    ("to do", "do"),
    ("todo", "do"),
    ("tasks", "do"),
    ("what to do", "do"),
    ("requirements", "do"),
    ("가드", "guard"),
    ("guards", "guard"),
    ("guard", "guard"),
    ("완료 기준", "done"),
    ("acceptance criteria", "done"),
    ("acceptance", "done"),
    ("done when", "done"),
    ("definition of done", "done"),
    ("하지 말 것", "constraint"),
    ("범위 밖", "constraint"),
    ("out of scope", "constraint"),
    ("constraints", "constraint"),
)

_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")
_BOLD_HEADING = re.compile(r"^\s*\*\*(?P<title>[^*]+?)\*\*\s*:?\s*$")
_CHECKBOX = re.compile(r"^\s*[-*+]\s+\[(?P<mark>[ xX])\]\s+(?P<text>.+?)\s*$")
_LIST_ITEM = re.compile(r"^(?P<indent>\s*)(?:[-*+]|\d+[.)])\s+(?P<text>.+?)\s*$")


def _heading_kind(title: str) -> str | None:
    cleaned = re.sub(r"\(.*?\)", "", title).strip().strip(":").strip().lower()
    for word, kind in _HEADING_KINDS:
        if cleaned == word:
            return kind
    return None


def _clip(text: str) -> str:
    return " ".join(text.split())[:_ITEM_MAX_CHARS]


def extract_requirements(text: str, *, source: str = "request") -> list[dict[str, str]]:
    """The checkable items of one request text (no ids — see :func:`number_requirements`).

    * an unticked checkbox (``- [ ] …``) is an item wherever it is; a ticked one is done;
    * a top-level list item directly under a recognised heading is an item of that kind;
    * everything else — prose, lists under other headings, nested bullets, anything inside a
      code fence — is not.
    """
    items: list[dict[str, str]] = []
    heading_kind: str | None = None
    list_indent: int | None = None
    in_fence = False
    for line in text.splitlines():
        # A fenced block is quoted material (a script, a config), never headings or items.
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = _MD_HEADING.match(line) or _BOLD_HEADING.match(line)
        if heading:
            heading_kind = _heading_kind(heading.group("title"))
            list_indent = None
            continue
        box = _CHECKBOX.match(line)
        if box:
            if box.group("mark") == " ":
                items.append(
                    {
                        "text": _clip(box.group("text")),
                        "kind": heading_kind or "do",
                        "source": source,
                    }
                )
            continue
        entry = _LIST_ITEM.match(line)
        if entry and heading_kind is not None:
            indent = len(entry.group("indent").expandtabs(4))
            if list_indent is None:
                list_indent = indent
            if indent <= list_indent:
                items.append(
                    {"text": _clip(entry.group("text")), "kind": heading_kind, "source": source}
                )
    return [i for i in items if i["text"]]


def number_requirements(items: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Give items stable ids (``R1``…), dropping repeats (same text, ignoring case/spacing)."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in items:
        text = _clip(str(item.get("text") or ""))
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        kind = str(item.get("kind") or "do")
        out.append(
            {
                "id": f"R{len(out) + 1}",
                "text": text,
                "kind": kind if kind in REQUIREMENT_KINDS else "do",
                "source": str(item.get("source") or "request"),
            }
        )
        if len(out) >= _MAX_ITEMS:
            break
    return out


def request_texts(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(source, text)`` pairs that make up the request.

    A comment on an issue (``issue_comment``) re-requests that ISSUE's work, and its
    checklist lives in the issue body — so the ledger reads the webhook where it carries
    them: ``body.issue.body`` and ``body.comment``. The comment counts only from a trusted
    author (OWNER / MEMBER / COLLABORATOR): on a public repo a stranger's comment is not an
    instruction. ``intent_text`` is not used here — it is the issue title plus the comment,
    whoever wrote it. Every other request is its own directive.
    """
    body = payload.get("body")
    if payload.get("github_event") == "issue_comment" and isinstance(body, Mapping):
        issue = body.get("issue")
        comment = body.get("comment")
        issue_map: Mapping[str, Any] = issue if isinstance(issue, Mapping) else {}
        comment_map: Mapping[str, Any] = comment if isinstance(comment, Mapping) else {}
        texts: list[tuple[str, str]] = []
        issue_text = str(issue_map.get("body") or "").strip()
        if issue_text:
            texts.append(("issue", issue_text))
        association = str(comment_map.get("author_association") or "").upper()
        comment_text = str(comment_map.get("body") or "").strip()
        if association in TRUSTED_ASSOCIATIONS and comment_text:
            texts.append(("comment", comment_text))
        return texts
    directive = str(payload.get("intent_text") or payload.get("text") or "").strip()
    source = "issue" if payload.get("github_event") == "issues" else "request"
    return [(source, directive)] if directive else []


def build_ledger(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """The request's ledger — empty when nothing in it is structured as an item."""
    found: list[dict[str, str]] = []
    for source, text in request_texts(payload):
        found.extend(extract_requirements(text, source=source))
    return number_requirements(found)


# ── evidence ─────────────────────────────────────────────────────────────────

_LINE_SUFFIX = re.compile(r"(?:#L\d+(?:-L?\d+)?|:\d+(?:-\d+)?)$")
_TEST_NAME = re.compile(r"^test[A-Za-z0-9_]{2,}$")


def _norm_path(raw: str) -> str:
    path = raw.strip().strip("`")
    path = _LINE_SUFFIX.sub("", path)
    for prefix in ("./", "a/", "b/", "/workspace/"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
    return path.strip("/")


def _str_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [s for s in (str(x).strip() for x in raw if isinstance(x, str | int)) if s]


def parse_requirement_evidence(raw: Any) -> list[dict[str, Any]]:
    """Normalise a ``requirement_evidence`` declaration. Unusable entries are dropped (that
    item then has no evidence); the LAST entry per id wins."""
    by_id: dict[str, dict[str, Any]] = {}
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, Mapping):
            continue
        rid = str(entry.get("requirement_id") or "").strip()
        if not rid:
            continue
        status = "deferred" if str(entry.get("status") or "").lower() == "deferred" else "done"
        by_id[rid] = {
            "requirement_id": rid,
            "status": status,
            "paths": [p for p in (_norm_path(p) for p in _str_list(entry.get("paths"))) if p],
            "tests": _str_list(entry.get("tests")),
            "note": str(entry.get("note") or entry.get("reason") or "").strip()[:500],
        }
    return list(by_id.values())


def merge_evidence(
    earlier: Sequence[Mapping[str, Any]], later: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """A re-declaration keeps the evidence of ids it does not mention."""
    merged = {str(e["requirement_id"]): dict(e) for e in earlier if e.get("requirement_id")}
    for entry in later:
        if entry.get("requirement_id"):
            merged[str(entry["requirement_id"])] = dict(entry)
    return list(merged.values())


#: JSON schema of ``declare_verification``'s ``requirement_evidence`` — one shape for the
#: in-process tool and the MCP work tool, so neither can advertise a field the other drops.
REQUIREMENT_EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": (
        "OPTIONAL unless the turn showed a REQUIREMENTS LEDGER — then one entry per item id. "
        "`paths`: files THIS run changed that satisfy the item (a path you did not change does "
        "not count). `tests`: tests that prove it, as `<test file>::<test name>` (counts when "
        "its file changed or a passing check ran it). `note`: one sentence on how. "
        "`status: deferred` + `note` (the reason) when the item cannot be done here."
    ),
    "items": {
        "type": "object",
        "properties": {
            "requirement_id": {"type": "string"},
            "status": {"type": "string", "enum": ["done", "deferred"]},
            "paths": {"type": "array", "items": {"type": "string"}},
            "tests": {"type": "array", "items": {"type": "string"}},
            "note": {"type": "string"},
        },
        "required": ["requirement_id"],
    },
}


@dataclass(frozen=True)
class GateFacts:
    """What the gate can vouch for: the paths this run changed (as git sees them, so shell
    edits count) and the commands that ran and passed."""

    diff_paths: frozenset[str]
    passed_commands: tuple[str, ...]


#: Test runners. Only a passed command that RUNS tests can vouch that the tests under a
#: directory it names ran — ``ruff check tests/`` or ``ls tests`` name the directory too, and
#: run none of them.
_TEST_RUNNER_WORDS = frozenset({"pytest", "vitest", "jest", "mocha", "playwright", "unittest"})


def _is_test_run(command: str) -> bool:
    words = command.split()
    for k, word in enumerate(words):
        if word.rsplit("/", 1)[-1] in _TEST_RUNNER_WORDS:
            return True
        # ``go test``, ``cargo test``, ``npm/pnpm/yarn test``, ``pnpm run test:unit``
        if (word == "test" or word.startswith("test:")) and k > 0:
            return True
    return False


def _runner_tokens(facts: GateFacts) -> set[str]:
    tokens: set[str] = set()
    for command in facts.passed_commands:
        if _is_test_run(command):
            tokens.update(_norm_path(token) for token in command.split())
    return tokens


def _in_diff(path: str, diff_paths: frozenset[str]) -> bool:
    """In this run's diff — or under a new directory git lists as a whole (``dir/``)."""
    return path in diff_paths or any(d.endswith("/") and path.startswith(d) for d in diff_paths)


def _test_grounded(test: str, facts: GateFacts, tokens: set[str]) -> bool:
    file_part, _, name = test.partition("::")
    file_path = _norm_path(file_part)
    if "::" in test or file_path.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rs")):
        if _in_diff(file_path, facts.diff_paths) or file_path in tokens:
            return True
        # A passed test run over a directory containing the file covers it.
        return any(t and file_path.startswith(t.rstrip("/") + "/") for t in tokens)
    # A bare test name only counts when a passed test run named it (e.g. ``-k test_x``).
    return bool(_TEST_NAME.match(name or test)) and test in tokens


def check_ledger(
    requirements: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    facts: GateFacts,
) -> list[dict[str, Any]]:
    """One row per item: ``missing`` / ``deferred`` / ``noted`` / ``pending`` (grounded, for
    the judge). Deterministic — the judge only ever sees ``pending`` rows."""
    by_id = {str(e.get("requirement_id")): e for e in evidence}
    tokens = _runner_tokens(facts)
    rows: list[dict[str, Any]] = []
    for item in requirements:
        row: dict[str, Any] = {
            "id": item["id"],
            "text": item["text"],
            "kind": item.get("kind", "do"),
            "source": item.get("source", "request"),
            "paths": [],
            "tests": [],
            "note": "",
        }
        entry = by_id.get(str(item["id"]))
        if row["kind"] not in OWED_KINDS:
            row["status"] = "noted"
        elif entry is not None and entry.get("status") == "deferred":
            row["note"] = str(entry.get("note") or "")
            if row["note"]:
                row["status"] = "deferred"
            else:
                row["status"] = "missing"
                row["note"] = "deferred without a reason — a deferral needs a `note` saying why"
        else:
            if entry is not None:
                row["paths"] = [
                    p for p in entry.get("paths") or [] if _in_diff(p, facts.diff_paths)
                ]
                row["tests"] = [
                    t for t in entry.get("tests") or [] if _test_grounded(t, facts, tokens)
                ]
                row["note"] = str(entry.get("note") or "")
            row["status"] = "pending" if row["paths"] or row["tests"] else "missing"
        rows.append(row)
    return rows


def ledger_passed(rows: Sequence[Mapping[str, Any]]) -> bool:
    """Every owed item is ``done``, ``unclear`` (judge could not tell) or ``deferred`` with a
    reason. ``missing``, ``rejected`` and an unjudged ``pending`` fail."""
    return all(r.get("status") in {"done", "unclear", "deferred", "noted"} for r in rows)


def ledger_closes(rows: Sequence[Mapping[str, Any]]) -> bool:
    """The PR may close its issue only when every owed item is ``done``."""
    return all(r.get("status") in {"done", "noted"} for r in rows)


def ledger_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "requirements_total": len(rows),
        "requirements_done": sum(1 for r in rows if r.get("status") in {"done", "unclear"}),
        "requirements_deferred": sum(1 for r in rows if r.get("status") == "deferred"),
    }


# ── words ────────────────────────────────────────────────────────────────────


def ledger_prompt(requirements: Sequence[Mapping[str, Any]]) -> str:
    """The ledger as the agent sees it at the start of every drive."""
    lines = [
        "REQUIREMENTS LEDGER — the request asked for these items. Verification FAILS while an "
        "item below (except a constraint) has no evidence.",
        "",
    ]
    for item in requirements:
        lines.append(f"- {item['id']} [{item.get('kind', 'do')}] {item['text']}")
    lines += [
        "",
        "When you call declare_verification, include `requirement_evidence`: one entry per id, "
        '`{"requirement_id": "R1", "paths": ["<file you changed>"], "tests": '
        '["<test file>::<test name>"], "note": "<how this satisfies the item>"}`. A path counts '
        "only if this run changed it; a test counts only if its file changed or a passing check "
        "ran it. If an item genuinely cannot be done here, send "
        '`{"requirement_id": "R3", "status": "deferred", "note": "<why>"}` — the pull request '
        "will then reference the issue instead of closing it. Constraints [constraint] need no "
        "entry: they are shown to the reviewer.",
    ]
    return "\n".join(lines)


_HUNK_START = re.compile(r"^@@ ", re.M)
_BACKTICKED = re.compile(r"`([^`]{3,80})`")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")


def relevant_hunks(diff: str, about: str, *, budget: int) -> str:
    """``diff`` cut to ``budget`` around what ``about`` names, instead of its head.

    A long diff's first hunks are rarely the ones an item is about. Hunks are ranked by the
    identifiers ``about`` mentions (backticked ones weigh most), kept in file order, and the
    rest are counted, never silently dropped. Nothing to match keeps the head."""
    if len(diff) <= budget:
        return diff
    starts = [m.start() for m in _HUNK_START.finditer(diff)]
    if not starts:
        return diff[:budget] + "\n[... cut ...]"
    header = diff[: starts[0]]
    hunks = [diff[a:b] for a, b in zip(starts, [*starts[1:], len(diff)], strict=True)]
    strong = {t for t in _BACKTICKED.findall(about)}
    weak = set(_IDENTIFIER.findall(about)) - strong

    def score(hunk: str) -> int:
        return sum(5 for t in strong if t in hunk) + sum(1 for t in weak if t in hunk)

    ranked = sorted(range(len(hunks)), key=lambda i: (-score(hunks[i]), i))
    keep: set[int] = set()
    used = len(header)
    for i in ranked:
        if used + len(hunks[i]) > budget:
            continue
        keep.add(i)
        used += len(hunks[i])
    omitted = len(hunks) - len(keep)
    shown = header + "".join(hunks[i] for i in sorted(keep))
    return shown + (f"\n[... {omitted} other hunk(s) omitted ...]" if omitted else "")


def unevidenced(
    requirements: Sequence[Mapping[str, Any]], evidence: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """Owed items the declaration has no entry for — not even a deferral."""
    named = {str(e.get("requirement_id")) for e in evidence}
    return [i for i in requirements if i.get("kind") in OWED_KINDS and i["id"] not in named]


def evidence_reminder(items: Sequence[Mapping[str, Any]]) -> str:
    """Asked once, before verifying, when the run says it is done with items unevidenced."""
    lines = [
        "REQUIREMENT EVIDENCE MISSING — you are about to be verified, and your "
        "declare_verification names no evidence for these ledger items. Verification FAILS "
        "for each one. Call declare_verification again with `requirement_evidence` for every "
        "item below (paths you changed, tests that run it, or `status: deferred` with a note), "
        "then finish.",
        "",
    ]
    lines += [f"- {i['id']} [{i.get('kind', 'do')}] {i['text']}" for i in items]
    return "\n".join(lines)


_STATUS_WORDS = {
    "missing": "no evidence",
    "rejected": "evidence does not satisfy it",
    "pending": "not judged (another check failed first)",
}


def render_ledger_failure(rows: Sequence[Mapping[str, Any]]) -> str:
    """The verification-failure section naming each unsatisfied item."""
    lines = ["REQUIREMENTS NOT SHOWN — each of these needs evidence in requirement_evidence:"]
    for row in rows:
        status = str(row.get("status"))
        if status in _STATUS_WORDS:
            detail = (
                f" — {row['note']}" if status in {"rejected", "missing"} and row.get("note") else ""
            )
            lines.append(f"- {row['id']} {row['text']} ({_STATUS_WORDS[status]}){detail}")
    return "\n".join(lines)


def _cell(text: Any) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")[:160]


def render_ledger_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """Markdown table for the PR body / report."""
    out = ["| | Item | Status | Evidence |", "|---|---|---|---|"]
    for row in rows:
        evidence = ", ".join([*row.get("paths", []), *row.get("tests", [])]) or row.get("note", "")
        out.append(
            f"| {row['id']} | {_cell(row['text'])} | {row.get('status')} | {_cell(evidence)} |"
        )
    return "\n".join(out)


def public_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rows as stored on the verification result (no excerpts)."""
    keep = ("id", "text", "kind", "source", "status", "paths", "tests", "note", "excerpt_hash")
    return [{k: r[k] for k in keep if k in r} for r in rows]


__all__ = [
    "evidence_reminder",
    "relevant_hunks",
    "unevidenced",
    "LEDGER_RESULT_KEY",
    "OWED_KINDS",
    "REQUIREMENTS_KEY",
    "REQUIREMENT_EVIDENCE_SCHEMA",
    "REQUIREMENT_KINDS",
    "TRUSTED_ASSOCIATIONS",
    "GateFacts",
    "build_ledger",
    "check_ledger",
    "extract_requirements",
    "ledger_closes",
    "ledger_counts",
    "ledger_passed",
    "ledger_prompt",
    "merge_evidence",
    "number_requirements",
    "parse_requirement_evidence",
    "public_rows",
    "render_ledger_failure",
    "render_ledger_table",
    "request_texts",
]
