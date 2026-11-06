"""Run the independent review of a PASSED verification round and hand back what to store.

Report-only (R5 stage 1): the review never changes the round's outcome. Whatever goes wrong —
no diff, the model errors, times out or babbles — the round keeps its verdict and the review is
``{"status": "unavailable", "reason": …}``. The same tree is never reviewed twice.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.application.requirements_ledger import run_requirements
from backend.workflow.domain.artifact_review import (
    MAX_SYMBOLS,
    NewSymbol,
    build_review_messages,
    new_public_symbols,
    parse_review,
    production_calls,
    tree_fingerprint,
    unavailable,
)
from backend.workflow.infrastructure.sandbox import SandboxError, SandboxSession

logger = structlog.get_logger(__name__)

REVIEW_KEY = "review"
REVIEW_TREE_KEY = "review_tree"
_REVIEW_TIMEOUT_S = 180.0
_GIT_TIMEOUT_S = 20.0
_PRIOR_ROUNDS = 10
# Pathspecs that keep tests out of the call-site search (the domain filter is the backstop).
_NOT_TESTS = (
    ":(exclude)tests",
    ":(exclude)test",
    ":(exclude)**/tests/**",
    ":(exclude)**/__tests__/**",
    ":(exclude)**/test_*.py",
    ":(exclude)**/*_test.py",
    ":(exclude)**/conftest.py",
    ":(exclude)**/*.test.*",
    ":(exclude)**/*.spec.*",
)

Complete = Callable[[Any, list[dict[str, Any]]], Awaitable[Any]]


async def review_passed_round(
    *,
    passed: bool,
    session: AsyncSession,
    release: Callable[[], Awaitable[None]],
    complete: Complete,
    run: Any,
    box: SandboxSession,
    baseline: str | None,
    ledger: list[dict[str, Any]] | None,
    command_results: list[dict[str, Any]],
    derived_gate: dict[str, Any] | None,
) -> dict[str, Any]:
    """The fields to merge into the round's ``VerificationResult.result`` — ``{}`` when no
    review applies (a failed round, or no baseline to diff against)."""
    if not passed or not baseline:
        return {}
    try:
        diff = await _git(box, f"diff {shlex.quote(baseline)} HEAD")
        tree = tree_fingerprint(diff)
        prior = await _prior_review(session, run.id, tree)
        await release()
        if prior is not None:
            return {REVIEW_KEY: prior, REVIEW_TREE_KEY: tree}
        symbols = new_public_symbols(diff)
        calls = {sym: await _production_calls(box, sym) for sym in symbols[:MAX_SYMBOLS]}
        payload = run.payload if isinstance(run.payload, dict) else {}
        messages = build_review_messages(
            request=str(payload.get("intent_text") or payload.get("text") or ""),
            ledger=ledger if ledger is not None else run_requirements(run),
            diff=diff,
            calls=calls,
            gate_commands=[*((derived_gate or {}).get("commands") or []), *command_results],
            symbols_cut=max(len(symbols) - MAX_SYMBOLS, 0),
        )
        turn = await asyncio.wait_for(complete(run, messages), timeout=_REVIEW_TIMEOUT_S)
        review = parse_review(str(getattr(turn, "content", "") or ""))
    except Exception as exc:  # noqa: BLE001 — a review hiccup must never change the verdict
        logger.warning("artifact_review_unavailable", run_id=str(run.id), error=repr(exc)[:200])
        return {REVIEW_KEY: unavailable(f"{type(exc).__name__}: {exc}"[:300])}
    if review is None:
        return {REVIEW_KEY: unavailable("리뷰어 출력을 해석하지 못했다")}
    return {REVIEW_KEY: review, REVIEW_TREE_KEY: tree}


async def _git(box: SandboxSession, args: str) -> str:
    result = await box.exec(  # noqa: S604 — every interpolated value is shlex-quoted
        f"git -C {shlex.quote(box.workspace_mount)} {args}", timeout_s=_GIT_TIMEOUT_S, shell=True
    )
    if result.timed_out or result.exit_code != 0:
        raise SandboxError(f"git {args.split(maxsplit=1)[0]} failed (exit {result.exit_code})")
    return result.stdout or ""


async def _production_calls(box: SandboxSession, symbol: NewSymbol) -> list[str] | None:
    """Where non-test code uses ``symbol``; ``None`` when git could not answer."""
    excludes = " ".join(shlex.quote(p) for p in _NOT_TESTS)
    try:
        result = await box.exec(  # noqa: S604 — every interpolated value is shlex-quoted
            f"git -C {shlex.quote(box.workspace_mount)} grep -n -w -e {shlex.quote(symbol.name)}"
            f" -- . {excludes}",
            timeout_s=_GIT_TIMEOUT_S,
            shell=True,
        )
    except SandboxError:
        return None
    if result.timed_out or result.exit_code not in (0, 1):  # 1 = no match
        return None
    return production_calls(symbol, result.stdout or "")


async def _prior_review(session: AsyncSession, run_id: Any, tree: str) -> dict[str, Any] | None:
    from backend.workflow.infrastructure.db import VerificationResult  # noqa: PLC0415

    rows = (
        await session.execute(
            select(VerificationResult.result)
            .where(VerificationResult.run_id == run_id)
            .order_by(VerificationResult.created_at.desc())
            .limit(_PRIOR_ROUNDS)
        )
    ).scalars()
    for result in rows:
        if not isinstance(result, dict) or result.get(REVIEW_TREE_KEY) != tree:
            continue
        review = result.get(REVIEW_KEY)
        if isinstance(review, dict) and review.get("status") == "done":
            return review
    return None
