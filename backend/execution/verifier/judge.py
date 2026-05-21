"""LLM-as-judge — executes a verification contract's ``judge`` checks.

A ``judge`` check carries concrete acceptance criteria the work LLM
committed to *before* doing the work. This module runs ONE verifier-LLM
call that grades each criterion pass/fail against the actual workspace
files. Anti-gaming (design
``~/Docs/BSNexus_Verification_Contract_Design_2026-05-17.md``):

- a separate call from the work LLM — never the work conversation;
- grades concrete declared criteria, never "is this good";
- sees the actual files on disk, not the work LLM's prose claims;
- an undecided / unparseable verdict → ``error`` → human_review_required.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from backend.execution._domain import ProofAspectStatus

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.core.executor_config.protocol
# from backend.src.core.executor_config.protocol import ExecutorClient

logger = structlog.get_logger(__name__)

# Keep the judge prompt bounded — a runaway workspace must not blow the
# context window.
_MAX_FILES = 40
_MAX_FILE_BYTES = 8_000
_MAX_TOTAL_BYTES = 120_000
_JUDGE_TEXT_EXTS = frozenset(
    {
        ".py",
        ".md",
        ".txt",
        ".toml",
        ".json",
        ".yaml",
        ".yml",
        ".cfg",
        ".ini",
        ".js",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".sh",
    }
)


@dataclass(frozen=True)
class JudgeContext:
    """Per-verification LLM access for the judge runner. ``None`` at the
    call site means no judge is available — judge checks then ``skip``."""

    executor: ExecutorClient
    model: str
    metadata: dict[str, Any]


def _render_workspace(root: Path) -> str:
    """Render the workspace files (capped) for the judge prompt."""
    if not root.is_dir():
        return "(workspace directory is empty or missing)"
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.suffix.lower() not in _JUDGE_TEXT_EXTS:
            continue
        files.append(path)
        if len(files) >= _MAX_FILES:
            break
    if not files:
        return "(no readable text files in the workspace)"
    chunks: list[str] = []
    total = 0
    for path in files:
        rel = path.relative_to(root)
        try:
            data = path.read_bytes()
        except OSError:
            continue
        text = data[:_MAX_FILE_BYTES].decode("utf-8", errors="replace")
        if len(data) > _MAX_FILE_BYTES:
            text += "\n…(truncated)"
        block = f"=== {rel} ===\n{text}"
        total += len(block)
        if total > _MAX_TOTAL_BYTES:
            chunks.append("…(remaining files omitted — workspace too large)")
            break
        chunks.append(block)
    return "\n\n".join(chunks)


def _parse_verdicts(text: str, n_criteria: int) -> list[dict[str, Any]] | None:
    """Extract the ``verdicts`` array from the judge's JSON reply."""
    start, end = text.find("{"), text.rfind("}")
    if not 0 <= start < end:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    verdicts = obj.get("verdicts") if isinstance(obj, dict) else None
    if not isinstance(verdicts, list) or len(verdicts) != n_criteria:
        return None
    return [v for v in verdicts if isinstance(v, dict)]


async def judge_criteria(
    *,
    criteria: tuple[str, ...],
    workspace_root: Path,
    judge: JudgeContext,
) -> tuple[ProofAspectStatus, str]:
    """Grade ``criteria`` against the workspace via one verifier-LLM
    call. Returns ``(status, summary)``:

      - ``passed`` — every criterion satisfied;
      - ``failed`` — at least one criterion not satisfied;
      - ``error``  — the judge could not pronounce (call failed or
        unparseable) → human_review_required at the deliverable level.
    """
    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(criteria))
    workspace = _render_workspace(workspace_root)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict, skeptical verifier for an AI company. You are given a work "
                "deliverable — the actual files in a workspace — and a numbered list of "
                "acceptance criteria the work was committed to BEFORE it was done. For EACH "
                "criterion decide pass or fail based ONLY on the file contents shown. A "
                "criterion passes only if the files concretely and verifiably satisfy it; if "
                "the evidence is absent, partial, or ambiguous, it FAILS. Do not give the "
                "benefit of the doubt. Output ONLY JSON, no prose:\n"
                '{"verdicts": [{"criterion": "<verbatim>", "pass": true|false, '
                '"reason": "<one sentence citing the file evidence>"}]}\n'
                "Return exactly one verdict per criterion, in order."
            ),
        },
        {
            "role": "user",
            "content": f"WORKSPACE FILES:\n{workspace}\n\nACCEPTANCE CRITERIA:\n{numbered}",
        },
    ]
    try:
        result = await judge.executor.execute(
            messages=messages,
            metadata=judge.metadata,
            model=judge.model,
            tools=None,
        )
        text = str(result.get("output_ref") or "")
    except Exception as exc:  # noqa: BLE001 — judge failure must not crash verification
        logger.warning("verification_judge_call_failed", error=str(exc))
        return ProofAspectStatus.error, f"llm judge call failed: {exc.__class__.__name__}"

    verdicts = _parse_verdicts(text, len(criteria))
    if verdicts is None:
        logger.warning("verification_judge_unparseable", reply_preview=text[:200])
        return ProofAspectStatus.error, "llm judge returned an unparseable verdict"

    failed = [v for v in verdicts if not bool(v.get("pass"))]
    if failed:
        lines = "; ".join(
            f"FAIL: {str(v.get('criterion') or '?')[:120]} — {str(v.get('reason') or '')[:200]}"
            for v in failed
        )
        return ProofAspectStatus.failed, f"{len(failed)}/{len(criteria)} criteria failed. {lines}"
    return ProofAspectStatus.passed, f"all {len(criteria)} judge criteria satisfied"


__all__ = ["JudgeContext", "judge_criteria"]
