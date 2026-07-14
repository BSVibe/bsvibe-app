"""Work tools — the agent's REMOTE hands on a run, over MCP (T1).

BSVibe's first principle is that an executor account and a LiteLLM account behave
IDENTICALLY through ``chat()``. The founder's clarification of it: **the executor is the
user's LLM client, not an execution environment.** State lives on the SERVER. An agent acts
on that state through BSVibe's own tools, executed server-side — never through the coding
CLI's own local tools in a temp dir the worker then scrapes back.

The scraping model is what produced (2026-07-14 parity audit): an agent that could not see
the product's code and so INVENTED it (captured, committed, merged); whole-file capture
reverting previously shipped work; a >256 KB file ZEROED in the founder's repo
(``raw = b"" if truncated`` → ``store.put``); and a file deletion 422-ing the entire result.
None of those failure modes can be expressed once the agent's only hands are these tools.

This module is a TRANSPORT, not a second implementation: every handler forwards to the same
:class:`~backend.workflow.infrastructure.tools.ToolRegistry` the in-process loop calls, bound
to the principal's run (``workspace_dir`` = the run's server-side worktree, ``sandbox`` = the
run's DinD session). One implementation, two transports — which is the whole point.

The MCP layer adds exactly one thing: **run scoping**. The run is read from the TOKEN
(:attr:`McpPrincipal.run_id`) and never from the arguments, so an agent cannot redirect a
write into another run's tree, and the founder's ordinary workspace token cannot reach into a
run at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field

from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry

logger = structlog.get_logger(__name__)


class WorkToolRegistry(Protocol):
    """The workflow ToolRegistry, bound to one run."""

    async def invoke(self, name: str, arguments: dict[str, Any]) -> str: ...


#: Builds the run-bound workflow registry. Injected so this module stays a transport: the
#: production factory resolves run → worktree + sandbox; tests pass a fake.
RegistryForRun = Callable[[uuid.UUID, ToolContext], Awaitable[WorkToolRegistry]]


class _WorkInput(BaseModel):
    """Base for every work-tool input.

    ``extra="forbid"`` is load-bearing, not hygiene: it is what refuses a ``run_id``
    argument. The run comes from the token.
    """

    model_config = ConfigDict(extra="forbid")


class FileReadInput(_WorkInput):
    path: str = Field(..., min_length=1, description="Path relative to the run workspace.")


class FileListInput(_WorkInput):
    path: str = Field(".", description="Directory relative to the run workspace.")


class FileWriteInput(_WorkInput):
    path: str = Field(..., min_length=1)
    content: str = Field(...)


class FileEditInput(_WorkInput):
    path: str = Field(..., min_length=1)
    old: str = Field(..., description="Exact text to replace.")
    new: str = Field(...)


class ShellExecInput(_WorkInput):
    command: str = Field(..., min_length=1, description="Runs in the run's sandbox.")


class DeclareVerificationInput(_WorkInput):
    model_config = ConfigDict(extra="allow")  # the contract's shape is the loop's, not ours


class KnowledgeSearchInput(_WorkInput):
    query: str = Field(..., min_length=1)


class AskUserQuestionInput(_WorkInput):
    question: str = Field(..., min_length=1)
    context: str = Field("")
    options: list[str] | None = Field(None)


class EmitDeliverableInput(_WorkInput):
    model_config = ConfigDict(extra="allow")  # the loop's own shape, not ours

    artifact_type: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)


class WorkToolOutput(BaseModel):
    result: str


def _run_of(ctx: ToolContext) -> uuid.UUID:
    """The run this token may act on — or a refusal.

    A workspace-wide token (the founder's editor, the CLI) is NOT allowed here: it could
    otherwise edit code inside any run. Only the short-lived token a dispatched executor task
    carries is run-scoped."""
    run_id: uuid.UUID | None = ctx.principal.run_id
    if run_id is None:
        raise ToolError(
            "this tool needs a run-scoped token — it is dispatched with an executor task, "
            "not issued to a workspace"
        )
    return run_id


#: What the agent is told after it asks the founder something. It is a courtesy, not the
#: mechanism: the loop terminates on the pending Decision whether or not the CLI obeys —
#: a coding CLI trusts its own tools over anything the prompt says (measured, 2026-07-13).
_STOP_AFTER_ASKING = (
    "The founder has been asked and the run is paused on their answer. STOP now — do not "
    "continue working. The run resumes with their decision."
)


#: The two LOOP-owned effects, injected from the composition root (``backend.api.main``).
#:
#: They are not imported here on purpose. ``create_decision`` and ``handle_emit_deliverable``
#: live in the workflow layer, and the latter reaches ``backend.api.v1.live_events`` for the
#: live-event bus — which the MCP import contract forbids this context from importing (the
#: same reason ``delivery_dispatcher`` is built in ``main.py`` and passed down). Injecting them
#: keeps this module a TRANSPORT: it decides who may act on which run, never what the act is.
RecordQuestion = Callable[[Any, Any, dict[str, Any]], Awaitable[str]]
RecordDeliverable = Callable[[Any, Any, dict[str, Any]], Awaitable[str]]


def register_work_tools(
    registry: ToolRegistry,
    *,
    registry_for_run: RegistryForRun,
    record_question: RecordQuestion,
    record_deliverable: RecordDeliverable,
) -> None:
    """Expose the run's ToolRegistry over MCP."""

    def _tool(
        *,
        name: str,
        inner: str,
        description: str,
        input_schema: type[BaseModel],
        write: bool,
    ) -> Tool:
        async def _handler(args: BaseModel, ctx: ToolContext) -> dict[str, str]:
            run_id = _run_of(ctx)
            work = await registry_for_run(run_id, ctx)
            if inner == "shell_exec" and getattr(work, "sandbox", None) is None:
                # No sandbox → the registry would fall back to a HOST subprocess, and this
                # transport's host is the API container. Refuse loudly rather than silently
                # running the agent's shell there.
                raise ToolError(
                    "this run has no sandbox — refusing to run shell_exec on the server host"
                )
            arguments = args.model_dump(exclude_none=False)
            logger.info("mcp_work_tool", tool=inner, run_id=str(run_id))
            result = await work.invoke(inner, arguments)
            return {"result": result}

        return Tool(
            name=name,
            description=description,
            input_schema=input_schema,
            output_schema=WorkToolOutput,
            handler=_handler,
            required_scopes=("mcp:write",) if write else ("mcp:read",),
        )

    for spec in (
        dict(
            name="bsvibe_work_file_read",
            inner="file_read",
            description="Read a text file from the run's workspace.",
            input_schema=FileReadInput,
            write=False,
        ),
        dict(
            name="bsvibe_work_file_list",
            inner="file_list",
            description="List directory entries in the run's workspace.",
            input_schema=FileListInput,
            write=False,
        ),
        dict(
            name="bsvibe_work_file_write",
            inner="file_write",
            description="Create or overwrite a file in the run's workspace.",
            input_schema=FileWriteInput,
            write=True,
        ),
        dict(
            name="bsvibe_work_file_edit",
            inner="file_edit",
            description="Replace an exact string in a file in the run's workspace.",
            input_schema=FileEditInput,
            write=True,
        ),
        dict(
            name="bsvibe_work_shell_exec",
            inner="shell_exec",
            description="Run a shell command inside the run's sandbox (cwd = the workspace).",
            input_schema=ShellExecInput,
            write=True,
        ),
        dict(
            name="bsvibe_work_declare_verification",
            inner="declare_verification",
            description=("Declare how this work will be verified. Required BEFORE writing files."),
            input_schema=DeclareVerificationInput,
            write=True,
        ),
        dict(
            name="bsvibe_work_knowledge_search",
            inner="knowledge_search",
            description="Search the workspace's settled knowledge mid-run.",
            input_schema=KnowledgeSearchInput,
            write=False,
        ),
    ):
        registry.register(_tool(**spec))  # type: ignore[arg-type]

    async def _h_ask(args: BaseModel, ctx: ToolContext) -> dict[str, str]:
        run_id = _run_of(ctx)
        payload = args.model_dump(exclude_none=True)
        logger.info("mcp_work_ask_user_question", run_id=str(run_id))
        await record_question(run_id, ctx, payload)
        return {"result": _STOP_AFTER_ASKING}

    async def _h_emit(args: BaseModel, ctx: ToolContext) -> dict[str, str]:
        run_id = _run_of(ctx)
        return {"result": await record_deliverable(run_id, ctx, args.model_dump())}

    registry.register(
        Tool(
            name="bsvibe_work_ask_user_question",
            description=(
                "Ask the founder a blocking question. The run PAUSES on their answer — stop "
                "working after calling this."
            ),
            input_schema=AskUserQuestionInput,
            output_schema=WorkToolOutput,
            handler=_h_ask,
            required_scopes=("mcp:write",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_work_emit_deliverable",
            description="Record a deliverable produced DURING the run (before it finishes).",
            input_schema=EmitDeliverableInput,
            output_schema=WorkToolOutput,
            handler=_h_emit,
            required_scopes=("mcp:write",),
        )
    )


__all__ = ["RegistryForRun", "WorkToolRegistry", "register_work_tools"]
