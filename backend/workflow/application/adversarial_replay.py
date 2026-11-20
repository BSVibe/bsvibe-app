"""Replay Stage A (the adversarial verifier) over a past deliverable.

The design doc (§4) measures the verifier over ~20 runs; runs with no known answer need a human
per card. A deliverable whose outcome is already known can instead be fed back through Stage A
and compared with that answer — as often as wanted. This module is that replay.

Contract (each point is pinned by ``tests/workflow/test_adversarial_replay.py``):

* The tree Stage A sees is the deliverable's ``payload["diff"]`` applied to the commit the diff
  was made against. That commit is the one recorded on the deliverable, else the nearest ``main``
  commit before the deliverable whose blobs match every ``index <pre>..<post>`` line. No commit,
  or a diff that does not apply, means the case is NOT run and the reason is returned — Stage A
  is never pointed at a guessed tree.
* The round is the production :func:`run_adversarial_round` (same judge LLM path, same copy /
  before-change control / time and token caps); its ``baseline`` is that commit.
* Nothing is written: no run, deliverable, Safe Mode card, ledger row or knowledge. The stand-in
  ``run`` is a plain namespace, so the production ``verification_quality`` aggregate cannot see
  it. Every tree is removed afterwards.
* One replay at a time (a second caller is refused, not queued), at most
  :data:`MAX_REPEAT` repeats and :data:`MAX_CASES` cases.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import structlog

from backend.workflow.application.adversarial_verifier import run_adversarial_round
from backend.workflow.infrastructure.sandbox.protocol import SandboxSession

logger = structlog.get_logger(__name__)

MAX_REPEAT = 5
MAX_CASES = 20
_MAX_CANDIDATES = 500  # how far back on main the base commit is searched
_GIT_TIMEOUT_S = 120.0
_ZEROS = re.compile(r"^0+$")
_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_INDEX_LINE = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)")
_RECORDED_BASE_KEYS = ("base_commit", "baseline", "base_sha")


class ReplayRefused(Exception):  # noqa: N818 — a refusal of the whole request, not an error
    """The request itself is refused (busy, too many cases / repeats)."""


class CaseUnavailable(Exception):  # noqa: N818 — one case cannot be replayed; carries why
    """This case is not run; the message is the reason handed back."""


@dataclass(frozen=True)
class ReplayInput:
    """What one past deliverable contributes to a replay."""

    diff: str
    diff_truncated: bool
    recorded_base: str | None
    created_at: datetime | None
    repo_dir: Path
    intent_text: str = ""


@dataclass(frozen=True)
class CaseSpec:
    deliverable_id: uuid.UUID
    expected: str | None = None  # "defect" | "clean" | None (plain replay)
    defect_note: str | None = None


BoxFactory = Callable[[Path], contextlib.AbstractAsyncContextManager[SandboxSession]]
StageA = Callable[..., Awaitable[dict[str, Any]]]
Loader = Callable[[uuid.UUID], Awaitable[ReplayInput]]

_slot = asyncio.Lock()  # one replay at a time — replay spends the workspace owner's tokens


@contextlib.asynccontextmanager
async def replay_slot() -> AsyncIterator[None]:
    if _slot.locked():
        raise ReplayRefused("다른 재생이 이미 돌고 있다 — 끝난 뒤 다시 시도하라")
    async with _slot:
        yield


# ---------------------------------------------------------------------------
# tree reconstruction
# ---------------------------------------------------------------------------
async def _git(*args: str, cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), _GIT_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        return 124, "git timed out"
    return proc.returncode or 0, out.decode(errors="replace")


def diff_blobs(diff: str) -> list[tuple[str, str]]:
    """``[(path, pre_blob)]`` for every file the diff changes that existed before it."""
    out: list[tuple[str, str]] = []
    path: str | None = None
    for line in diff.splitlines():
        header = _DIFF_HEADER.match(line)
        if header:
            path = header.group(1)
            continue
        idx = _INDEX_LINE.match(line)
        if idx and path is not None:
            if not _ZEROS.match(idx.group(1)):  # a new file has no pre-image to match
                out.append((path, idx.group(1)))
            path = None
    return out


async def _commit_matches(repo: Path, commit: str, blobs: list[tuple[str, str]]) -> bool:
    code, out = await _git("rev-parse", *[f"{commit}:{p}" for p, _ in blobs], cwd=repo)
    if code != 0:
        return False
    shas = out.split()
    return len(shas) == len(blobs) and all(
        sha.startswith(pre) for sha, (_, pre) in zip(shas, blobs, strict=True)
    )


async def find_base_commit(repo: Path, inp: ReplayInput) -> str:
    """The commit the diff was made against; raises :class:`CaseUnavailable` when unknown."""
    if inp.recorded_base:
        code, out = await _git("rev-parse", "--verify", f"{inp.recorded_base}^{{commit}}", cwd=repo)
        if code != 0:
            raise CaseUnavailable(f"기록된 기준 커밋 {inp.recorded_base} 이(가) 저장소에 없다")
        return out.strip()
    blobs = diff_blobs(inp.diff)
    if not blobs:
        raise CaseUnavailable(
            "기준 커밋이 기록돼 있지 않고, diff 에 기존 파일의 index blob 이 없다"
        )
    args = ["rev-list", "--first-parent", "-n", str(_MAX_CANDIDATES)]
    if inp.created_at is not None:
        args.append(f"--before={inp.created_at.isoformat()}")
    code, out = await _git(*args, "main", cwd=repo)
    if code != 0:
        raise CaseUnavailable(f"main 이력을 읽지 못했다: {out.strip()[:200]}")
    for commit in out.split():  # newest first = nearest before the deliverable
        if await _commit_matches(repo, commit, blobs):
            return commit
    raise CaseUnavailable("diff 의 index blob 이 모두 맞는 main 커밋을 찾지 못했다")


async def reconstruct_tree(inp: ReplayInput, dest: Path) -> str:
    """Check out the base commit into ``dest`` and apply the diff there; returns the commit.

    ``dest`` is a shared-object clone, so the product's own repository is never touched."""
    if inp.diff_truncated:
        raise CaseUnavailable("diff 가 잘려 저장돼 있어 같은 트리를 만들 수 없다")
    if not inp.diff.strip():
        raise CaseUnavailable("결과물에 diff 가 없다")
    base = await find_base_commit(inp.repo_dir, inp)
    code, out = await _git(
        "clone", "--shared", "--no-checkout", str(inp.repo_dir), str(dest), cwd=inp.repo_dir
    )
    if code != 0:
        raise CaseUnavailable(f"저장소 사본을 만들지 못했다: {out.strip()[:200]}")
    code, out = await _git("checkout", "--detach", base, cwd=dest)
    if code != 0:
        raise CaseUnavailable(f"기준 커밋을 체크아웃하지 못했다: {out.strip()[:200]}")
    patch = dest.parent / f"{dest.name}.patch"
    patch.write_text(inp.diff if inp.diff.endswith("\n") else inp.diff + "\n")
    try:
        code, out = await _git("apply", "--whitespace=nowarn", str(patch), cwd=dest)
    finally:
        patch.unlink(missing_ok=True)
    if code != 0:
        raise CaseUnavailable(
            f"diff 가 기준 커밋 {base[:12]} 에 적용되지 않는다: {out.strip()[:200]}"
        )
    return base


# ---------------------------------------------------------------------------
# one replay (repeat × Stage A)
# ---------------------------------------------------------------------------
def _confirmed(blob: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"claim": f.get("claim"), "file": f.get("file"), "line": f.get("line")}
        for f in blob.get("findings") or []
        if isinstance(f, dict) and f.get("confirmed")
    ]


async def _remove(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, True)
    await asyncio.to_thread(shutil.rmtree, path.parent / f"{path.name}.patch", True)


async def _replay_once(
    inp: ReplayInput,
    *,
    llm: Any,
    box_for: BoxFactory,
    stage_a: StageA,
    work_root: Path,
) -> dict[str, Any]:
    """One Stage A round on its own freshly built tree (removed afterwards)."""
    dest = work_root / f"replay-{uuid.uuid4().hex[:12]}"
    try:
        base = await reconstruct_tree(inp, dest)
        run = SimpleNamespace(
            id=uuid.uuid4(),
            payload={"intent_text": inp.intent_text},
            usage_prompt_tokens=0,
            usage_completion_tokens=0,
        )

        async def release() -> None:
            return None

        started = time.monotonic()
        async with box_for(dest) as box:
            blob = await stage_a(
                llm=llm, run=run, box=box, baseline=base, surface="code", release=release
            )
        return {
            "base_commit": base,
            "stage_a": blob,
            "confirmed_findings": _confirmed(blob),
            "tokens": {
                "prompt": run.usage_prompt_tokens,
                "completion": run.usage_completion_tokens,
                "total": run.usage_prompt_tokens + run.usage_completion_tokens,
            },
            "seconds": round(time.monotonic() - started, 2),
        }
    finally:
        await _remove(dest)


async def _replay_case(
    spec: CaseSpec,
    *,
    repeat: int,
    load: Loader,
    llm: Any,
    box_for: BoxFactory,
    stage_a: StageA,
    work_root: Path,
) -> dict[str, Any]:
    head: dict[str, Any] = {
        "deliverable_id": str(spec.deliverable_id),
        "expected": spec.expected,
        **({"defect_note": spec.defect_note} if spec.defect_note else {}),
    }
    try:
        inp = await load(spec.deliverable_id)
        runs = []
        for _ in range(repeat):
            runs.append(
                await _replay_once(
                    inp, llm=llm, box_for=box_for, stage_a=stage_a, work_root=work_root
                )
            )
    except CaseUnavailable as exc:
        return {**head, "status": "skipped", "reason": str(exc), "runs": []}
    confirmed_runs = sum(1 for r in runs if r["stage_a"].get("confirmed"))
    return {
        **head,
        "status": "ran",
        "repeat": repeat,
        "confirmed_runs": confirmed_runs,
        "any_confirmed": confirmed_runs > 0,
        "runs": runs,
    }


def score(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over the cases that actually ran. Whether a confirmed finding is the SAME defect as
    the known one cannot be decided here — the findings are returned for a human to judge."""
    ran = [r for r in results if r["status"] == "ran"]
    defect = [r for r in ran if r["expected"] == "defect"]
    clean = [r for r in ran if r["expected"] == "clean"]
    runs = [run for r in ran for run in r["runs"]]

    def rate(rows: list[dict[str, Any]]) -> float | None:
        return sum(1 for r in rows if r["any_confirmed"]) / len(rows) if rows else None

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    return {
        "cases": len(results),
        "skipped": len(results) - len(ran),
        "defect_cases": len(defect),
        "defect_cases_confirmed": sum(1 for r in defect if r["any_confirmed"]),
        "defect_confirmed_rate": rate(defect),
        "clean_cases": len(clean),
        "clean_cases_confirmed": sum(1 for r in clean if r["any_confirmed"]),
        "false_confirmed_rate": rate(clean),
        "mean_tokens": mean([run["tokens"]["total"] for run in runs]),
        "mean_seconds": mean([run["seconds"] for run in runs]),
    }


async def run_replay(
    cases: list[CaseSpec],
    *,
    repeat: int = 1,
    load: Loader,
    llm: Any,
    box_for: BoxFactory,
    work_root: Path,
    stage_a: StageA = run_adversarial_round,
) -> dict[str, Any]:
    """Replay ``cases`` (one at a time process-wide). Raises :class:`ReplayRefused` on a bad or
    concurrent request; an unreplayable case is reported in the result, never raised."""
    if not 1 <= repeat <= MAX_REPEAT:
        raise ReplayRefused(f"repeat 는 1~{MAX_REPEAT} 이어야 한다")
    if not 1 <= len(cases) <= MAX_CASES:
        raise ReplayRefused(f"cases 는 1~{MAX_CASES} 개여야 한다")
    async with replay_slot():
        await asyncio.to_thread(work_root.mkdir, parents=True, exist_ok=True)
        results = [
            await _replay_case(
                spec,
                repeat=repeat,
                load=load,
                llm=llm,
                box_for=box_for,
                stage_a=stage_a,
                work_root=work_root,
            )
            for spec in cases
        ]
    return {"results": results, "summary": score(results)}


# ---------------------------------------------------------------------------
# reading the deliverable (read-only)
# ---------------------------------------------------------------------------
async def load_replay_input(
    session: Any, workspace_id: uuid.UUID, deliverable_id: uuid.UUID
) -> ReplayInput:
    """Read what a replay needs. Read-only; another workspace's deliverable reads as absent."""
    from backend.storage.product_workspace import product_workspace_path  # noqa: PLC0415
    from backend.workflow.domain.verified_deliverable import diff_of  # noqa: PLC0415
    from backend.workflow.infrastructure.db import Deliverable, ExecutionRun  # noqa: PLC0415

    row = await session.get(Deliverable, deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise CaseUnavailable("결과물을 찾을 수 없다")
    payload = row.payload if isinstance(row.payload, dict) else {}
    diff, truncated = diff_of(payload)
    run = await session.get(ExecutionRun, row.run_id)
    if run is None or run.product_id is None:
        raise CaseUnavailable("결과물의 런/제품을 찾을 수 없다")
    repo = product_workspace_path(run.product_id)
    if not (repo / ".git").exists():
        raise CaseUnavailable("제품 저장소가 이 서버에 없다")
    recorded = next(
        (
            str(payload[k])
            for k in _RECORDED_BASE_KEYS
            if isinstance(payload.get(k), str) and payload[k]
        ),
        None,
    )
    run_payload = run.payload if isinstance(run.payload, dict) else {}
    return ReplayInput(
        diff=diff or "",
        diff_truncated=truncated,
        recorded_base=recorded,
        created_at=row.created_at,
        repo_dir=repo,
        intent_text=str(run_payload.get("intent_text") or ""),
    )


__all__ = [
    "MAX_CASES",
    "MAX_REPEAT",
    "CaseSpec",
    "CaseUnavailable",
    "ReplayInput",
    "ReplayRefused",
    "load_replay_input",
    "run_replay",
    "score",
]
