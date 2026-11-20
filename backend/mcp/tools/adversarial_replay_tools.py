"""``bsvibe_adversarial_replay`` / ``bsvibe_adversarial_replay_status`` — Stage A replay as a job.

A replay cannot finish inside an MCP request (one Stage A round is up to 180 s; Cloudflare cuts a
silent request at ~100 s — see ``reindex_tools.py``). So ``bsvibe_adversarial_replay`` only
registers a job and returns its ``replay_id`` at once; the worker process runs it; and
``bsvibe_adversarial_replay_status`` reads progress, per-case results and the scoring from the DB
(they survive a restart). The rules (tree reconstruction, the production round, no side effects,
one at a time, scoring) live in :mod:`backend.workflow.application.adversarial_replay` and
:mod:`backend.workflow.application.adversarial_replay_jobs`. REST is deliberately not added: this
is a measurement tool for the founder's MCP client.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.workflow.application.adversarial_replay import (
    MAX_CASES,
    MAX_REPEAT,
    CaseSpec,
    ReplayRefused,
)
from backend.workflow.application.adversarial_replay_jobs import job_status, submit_job


class _Out(RootModel[Any]):
    pass


class _Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deliverable_id: uuid.UUID
    expected: Literal["defect", "clean"]
    defect_note: str | None = Field(default=None, max_length=2000)


class AdversarialReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deliverable_id: uuid.UUID | None = None
    repeat: int = Field(default=1, ge=1, le=MAX_REPEAT)
    cases: list[_Case] | None = Field(default=None, max_length=MAX_CASES)


class AdversarialReplayStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replay_id: uuid.UUID


async def _h_adversarial_replay(args: AdversarialReplayInput, ctx: ToolContext) -> Any:
    if (args.deliverable_id is None) == (args.cases is None):
        raise ToolError("deliverable_id 와 cases 중 하나만 주어야 한다")
    specs = (
        [CaseSpec(args.deliverable_id)]
        if args.deliverable_id is not None
        else [CaseSpec(c.deliverable_id, c.expected, c.defect_note) for c in args.cases or []]
    )
    try:
        job = await submit_job(
            ctx.session,
            ctx.principal.workspace_id,
            specs,
            repeat=args.repeat,
            batch=args.cases is not None,
        )
    except ReplayRefused as exc:
        raise ToolError(str(exc)) from exc
    return _Out(
        {
            "replay_id": str(job.id),
            "status": job.status,
            "cases": len(specs),
            "repeat": args.repeat,
            "next": "bsvibe_adversarial_replay_status(replay_id) 로 진행·결과를 조회하라",
        }
    )


async def _h_adversarial_replay_status(args: AdversarialReplayStatusInput, ctx: ToolContext) -> Any:
    out = await job_status(ctx.session, ctx.principal.workspace_id, args.replay_id)
    if out is None:
        raise ToolError("재생을 찾을 수 없다")
    return _Out(out)


def register_adversarial_replay_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_adversarial_replay",
            description=(
                "Register a background replay of the adversarial verifier (Stage A) over past "
                "deliverables, in a tree rebuilt from each deliverable's diff on its base commit. "
                f"Give `deliverable_id` (+ `repeat`, up to {MAX_REPEAT}) or `cases` (up to "
                f"{MAX_CASES}, each with expected 'defect' | 'clean') for a scored batch. Returns "
                "`replay_id` AT ONCE — the worker runs it; poll "
                "`bsvibe_adversarial_replay_status`. Creates no run, deliverable, card, ledger "
                "entry or knowledge; one replay at a time (a second is refused while one is "
                "queued/running); spends the workspace's judge-route tokens."
            ),
            input_schema=AdversarialReplayInput,
            output_schema=_Out,
            handler=_h_adversarial_replay,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.adversarial_replay.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_adversarial_replay_status",
            description=(
                "Progress, per-case results and scoring of a replay registered by "
                "`bsvibe_adversarial_replay`. Each case is `ran` (every repeat's Stage A blob, "
                "tokens, seconds, and the confirmed findings' claim/file/line for a human to "
                "judge), `skipped` (base commit / diff unusable — with the reason) or `failed` "
                "(an exception — with the reason). `summary` holds defect_confirmed_rate and "
                "false_confirmed_rate over the cases finished so far. A case with no `reason` "
                "ran; whether a confirmed finding is the KNOWN defect is for a human."
            ),
            input_schema=AdversarialReplayStatusInput,
            output_schema=_Out,
            handler=_h_adversarial_replay_status,
            required_scopes=("mcp:read",),
        )
    )


__all__ = ["register_adversarial_replay_tools"]
