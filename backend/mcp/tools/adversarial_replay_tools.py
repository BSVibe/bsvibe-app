"""``bsvibe_adversarial_replay`` — run Stage A again over past deliverables with known answers.

The rule (tree reconstruction, the production round, no side effects, one at a time, scoring)
lives in :mod:`backend.workflow.application.adversarial_replay`. REST is deliberately not added:
this is a measurement tool for the founder's MCP client, and a REST twin would only duplicate it.

The judge LLM is the workspace's own ``workflow.judge`` route — the same one a real verification
grades on — so the tokens are the workspace owner's.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.workflow.application.adversarial_replay import (
    MAX_CASES,
    MAX_REPEAT,
    CaseSpec,
    CaseUnavailable,
    ReplayRefused,
    load_replay_input,
    run_replay,
)
from backend.workflow.infrastructure.sandbox.protocol import SandboxSession


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


def _box_factory(settings: Any) -> Any:
    from backend.workflow.application.runtime.sandbox_selection import (  # noqa: PLC0415
        resolve_sandbox_manager,
    )

    manager = resolve_sandbox_manager(None, settings)

    @contextlib.asynccontextmanager
    async def _box(path: Path) -> AsyncIterator[SandboxSession]:
        project = uuid.uuid4()
        box = await manager.acquire(project, str(path))
        try:
            yield box
        finally:
            await manager.release(project)

    return _box


async def _h_adversarial_replay(args: AdversarialReplayInput, ctx: ToolContext) -> Any:
    from types import SimpleNamespace  # noqa: PLC0415

    from backend.config import get_settings  # noqa: PLC0415
    from backend.workers.emit import get_emit_redis_client  # noqa: PLC0415
    from backend.workflow.application.runtime.account_resolution import (  # noqa: PLC0415
        resolve_judge_llm,
    )

    if (args.deliverable_id is None) == (args.cases is None):
        raise ToolError("deliverable_id 와 cases 중 하나만 주어야 한다")
    specs = (
        [CaseSpec(args.deliverable_id)]
        if args.deliverable_id is not None
        else [CaseSpec(c.deliverable_id, c.expected, c.defect_note) for c in args.cases or []]
    )
    workspace_id = ctx.principal.workspace_id
    settings = get_settings()
    llm = await resolve_judge_llm(
        ctx.session,
        SimpleNamespace(id=uuid.uuid4(), workspace_id=workspace_id),  # type: ignore[arg-type]
        settings,
        redis=get_emit_redis_client(settings),
        session_factory=ctx.session_factory,
    )
    if llm is None:
        raise ToolError("이 워크스페이스에 judge 모델 경로가 없어 재생할 수 없다")

    async def load(deliverable_id: uuid.UUID) -> Any:
        try:
            return await load_replay_input(ctx.session, workspace_id, deliverable_id)
        finally:
            # Read-only: end the read transaction so a long replay holds no connection state.
            await ctx.session.rollback()

    root = Path(settings.run_workspace_root)
    root = root if root.is_absolute() else Path.cwd() / root
    try:
        out = await run_replay(
            specs,
            repeat=args.repeat,
            load=load,
            llm=llm,
            box_for=_box_factory(settings),
            work_root=root / "adversarial-replay",
        )
    except ReplayRefused as exc:
        raise ToolError(str(exc)) from exc
    except CaseUnavailable as exc:  # pragma: no cover — run_replay reports these per case
        raise ToolError(str(exc)) from exc
    # A single deliverable answers with its case directly; a batch with results + summary.
    return _Out(out["results"][0] if args.deliverable_id is not None else out)


def register_adversarial_replay_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_adversarial_replay",
            description=(
                "Run the adversarial verifier (Stage A) again over past deliverables, in a tree "
                "rebuilt from each deliverable's diff on its base commit. Give `deliverable_id` "
                f"(+ `repeat`, up to {MAX_REPEAT}) or `cases` "
                f"(up to {MAX_CASES}, each with expected 'defect' | 'clean') for a scored batch. "
                "Creates no run, deliverable, card, ledger entry or knowledge; one replay at a "
                "time; spends the workspace's judge-route tokens. A case whose base commit or "
                "diff cannot be reconstructed is skipped with the reason. Confirmed findings come "
                "back with claim/file/line — whether one is the known defect is for a human."
            ),
            input_schema=AdversarialReplayInput,
            output_schema=_Out,
            handler=_h_adversarial_replay,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.adversarial_replay.invoked",
        )
    )


__all__ = ["register_adversarial_replay_tools"]
