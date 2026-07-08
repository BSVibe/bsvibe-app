"""G6.6 — workspace-scoped tool registry for the dispatcher tool loop.

Handlers:
  - ``file_read(path)`` — read text file under workspace_dir
  - ``file_list(path)``  — list directory entries under workspace_dir
  - ``file_write(path, content)`` — create/overwrite file under workspace_dir
  - ``file_edit(path, old_string, new_string)`` — surgical exact-string
    replacement; requires the file was ``file_read`` first
  - ``shell_exec(command)`` — run a shell command with cwd=workspace_dir,
    30s timeout, denylist of destructive/network patterns

Every handler refuses to step outside ``workspace_dir`` via path
traversal (``..``, absolute paths). Per CLAUDE.md "Async everywhere",
all I/O is async (asyncio.create_subprocess_exec for shell_exec,
sync filesystem I/O wrapped in ``asyncio.to_thread``).

The registry returns OpenAI-style ``tools=[...]`` JSON schemas that
``LlmClient.complete(tools=...)`` forwards directly to LiteLLM.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from backend.workflow.infrastructure.sandbox import SandboxError, SandboxSession

if TYPE_CHECKING:
    from backend.knowledge.extraction.worth_remembering import RememberableKnowledge

logger = structlog.get_logger(__name__)


SHELL_DENYLIST_PATTERNS: tuple[str, ...] = (
    "rm -rf",
    "rm -fr",
    "rm -r ",
    " rm /",
    "sudo ",
    "curl ",
    "wget ",
    "ssh ",
    "scp ",
    "nc ",
    "ncat ",
    "telnet ",
    ":(){",
    "mkfs",
    "dd ",
    "/dev/sd",
    " > /dev/",
    "shutdown",
    "reboot",
    "halt",
    "kill -9 -1",
    "chmod 777 /",
)
SHELL_TIMEOUT_S: float = 30.0
FILE_READ_MAX_BYTES: int = 256 * 1024
FILE_WRITE_MAX_BYTES: int = 256 * 1024

# B7 — verify-first gate. The mutating file tools refuse until a verification
# contract has been declared this run, with this actionable message naming the
# unlock tool. The verification plan must be committed BEFORE the work, not
# bolted on after (or never) — TDD discipline, enforced not merely nudged.
VERIFY_FIRST_REFUSAL: str = (
    "Declare your verification first: call declare_verification(...) describing "
    "how this work will be checked (a command check that runs the real test/lint, "
    "scoped to the files you change), then write."
)


class ToolError(Exception):
    """Raised when a tool refuses to run (path escape, denylist hit,
    timeout, size cap). The dispatcher surfaces the message back to the
    LLM as the tool result so it can recover."""


@dataclass(frozen=True)
class ToolDefinition:
    """Schema + handler for one tool."""

    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]


class ToolRegistry:
    """Workspace-scoped tool registry. One instance per RunAttempt —
    holds the workspace root and stateful denylist enforcement.
    """

    def __init__(self, *, workspace_dir: Path, sandbox: SandboxSession | None = None) -> None:
        self._root = workspace_dir.resolve()
        # Part B — when a sandbox session is supplied, shell_exec and the
        # file tools run via ``docker exec`` inside the project sandbox
        # instead of host subprocesses. ``None`` keeps the host path.
        self._sandbox = sandbox
        self._tools: dict[str, ToolDefinition] = {}
        # The most recent verification contract declared via the
        # ``declare_verification`` tool, normalized to a JSON dict. The
        # dispatcher reads this after the work phase and persists it on
        # ``RunAttempt.verification_contract``. ``None`` means the work
        # LLM never declared one.
        self.declared_contract: dict[str, Any] | None = None
        # v2 (agent-authored knowledge): the knowledge the WORKING agent
        # declared alongside its contract — a retrospective-style
        # ``knowledge`` block naming what it learned, captured IN THE MOMENT
        # (full working context). ``None`` when the agent declared none
        # (routine work). The settle path writes this as a topic-titled note;
        # there is no post-hoc extractor.
        self.declared_knowledge: RememberableKnowledge | None = None
        # Paths the LLM has grounded itself in this attempt — via
        # ``file_read`` (saw the content) or ``file_write`` (supplied
        # it). ``file_edit`` requires the path to be here so a local
        # model edits against real content, not a hallucinated recall.
        self._grounded_paths: set[str] = set()
        self._register_defaults()

    def schema_for(self, names: list[str]) -> list[dict[str, Any]]:
        """Return OpenAI-style ``tools=[...]`` JSON for the given
        tool names — phase gating decides the slice."""
        result: list[dict[str, Any]] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                continue
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters_schema,
                    },
                }
            )
        return result

    async def invoke(self, name: str, arguments: dict[str, Any]) -> str:
        """Run a tool by name. Returns the result string the dispatcher
        appends as the tool message."""
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name!r}")
        return await tool.handler(arguments)

    def has(self, name: str) -> bool:
        return name in self._tools

    def register(self, definition: ToolDefinition) -> None:
        """Add a custom tool to the registry.

        Used by Bundle S to register ``invoke_skill`` after the registry is
        constructed (the skill loader is per-workspace and lives outside
        this module). Re-registering an existing name raises ``ToolError``.
        """
        if definition.name in self._tools:
            raise ToolError(f"tool already registered: {definition.name!r}")
        self._tools[definition.name] = definition

    def names(self) -> list[str]:
        """All registered tool names — for ``schema_for(names())`` callers."""
        return list(self._tools.keys())

    def _register_defaults(self) -> None:
        self._tools["file_read"] = ToolDefinition(
            name="file_read",
            description="Read a UTF-8 text file inside the workspace.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace root.",
                    },
                },
                "required": ["path"],
            },
            handler=self._file_read,
        )
        self._tools["file_list"] = ToolDefinition(
            name="file_list",
            description="List files and directories under a workspace path.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to the workspace root (default: '.').",
                        "default": ".",
                    },
                },
            },
            handler=self._file_list,
        )
        self._tools["file_write"] = ToolDefinition(
            name="file_write",
            description=(
                "Create or overwrite a UTF-8 text file inside the workspace. Parent directories are created as needed."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace root.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file contents to write.",
                    },
                },
                "required": ["path", "content"],
            },
            handler=self._file_write,
        )
        self._tools["file_edit"] = ToolDefinition(
            name="file_edit",
            description=(
                "Make a surgical edit to an EXISTING file: replace an exact string with "
                "another. ALWAYS prefer this over file_write when modifying a file that "
                "already exists — file_write replaces the whole file, and rewriting a "
                "large file from memory drops or corrupts the parts you did not mean to "
                "touch. file_edit changes only what you specify. You MUST file_read the "
                "file first (so old_string matches the real content). old_string must "
                "match the file EXACTLY — whitespace and indentation included — and be "
                "UNIQUE in the file (include enough surrounding context to make it so), "
                "or set replace_all to change every occurrence."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace root.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to replace — must occur in the file, uniquely unless replace_all.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Text to replace it with. Must differ from old_string.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence instead of requiring a unique match (default false).",
                        "default": False,
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
            handler=self._file_edit,
        )
        self._tools["shell_exec"] = ToolDefinition(
            name="shell_exec",
            description=(
                "Run a shell command with cwd=workspace_root, 30s timeout. Destructive / network commands are refused."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command line.",
                    },
                },
                "required": ["command"],
            },
            handler=self._shell_exec,
        )
        self._tools["declare_verification"] = ToolDefinition(
            name="declare_verification",
            description=(
                "Declare HOW this work step will be verified — call this early, before "
                "writing code, as a TDD-style commitment. Provide a list of checks. A "
                "'command' check is a shell command whose exit code is the verdict (exit "
                "0 = pass) — e.g. running the test suite or a linter. A 'judge' check is "
                "for non-executable criteria (docs, design): a list of concrete, "
                "independently checkable statements an LLM reviewer will grade. Declare "
                "test, lint, and build as separate command checks where they apply. "
                "A check that only compiles or imports a file (py_compile, a bare "
                "import) does NOT exercise its behaviour — when the step has tests, "
                "declare a command that RUNS the test runner, never one that merely "
                "compiles the test file. Weak (rejected as no real verification): "
                "`python -m py_compile test_calc.py`. Strong: `uv run pytest test_calc.py`. "
                "RUN TOOLS THROUGH THE PROJECT RUNNER so they resolve in the sandbox: "
                "for a uv/Python project use `uv run pytest …` / `uv run ruff …` — a bare "
                "`pytest` or `python -m pytest` may not be on the sandbox venv path and "
                "fails with 'No module named pytest'; for Node use the package script "
                "(`pnpm test`, `npm run lint`). "
                "SCOPE every command to the files you changed, never the whole repo: "
                "a repo-wide lint/format (e.g. `ruff check .`, `pnpm lint`) fails on "
                "pre-existing debt you did not touch, and trying to satisfy it "
                "rewrites unrelated files. Pass the changed paths explicitly — e.g. "
                "`uv run ruff check src/foo.py`, `uv run pytest tests/test_foo.py`. "
                "FORMAT your changed files before declaring done (the project quality "
                "gate runs `ruff format --check` on them) — e.g. `uv run ruff format "
                "src/foo.py tests/test_foo.py`. "
                "You may call this again to refine the contract."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "checks": {
                        "type": "array",
                        "description": "The verification checks for this work step.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": ["command", "judge"],
                                    "description": "'command' for an executable check, 'judge' for an LLM-graded one.",
                                },
                                "command": {
                                    "type": "string",
                                    "description": "Shell command to run (kind=command). Exit 0 = pass.",
                                },
                                "criteria": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Concrete checkable statements (kind=judge).",
                                },
                                "rationale": {
                                    "type": "string",
                                    "description": (
                                        "A SHORT plain-language note of WHAT this check "
                                        "verifies and WHY it matters — written for the "
                                        "non-developer founder, NOT jargon. This is SHOWN in "
                                        "the delivery report next to the check, so make it "
                                        "meaningful (e.g. 'Confirms the discount never goes "
                                        "negative', not 'runs the tests'). Write it in the "
                                        "founder's language."
                                    ),
                                },
                            },
                            "required": ["kind"],
                        },
                    },
                    "knowledge": {
                        "type": "object",
                        "description": (
                            "OPTIONAL — record what you LEARNED doing this work, IF it "
                            "is worth remembering: a non-obvious learning (a gotcha, a "
                            "constraint you discovered, why you chose one approach over "
                            "another) or a decision future work must honour. OMIT for "
                            "routine work (adding a utility, fixing a typo) — that is the "
                            "common case. Only you, who did the work, can see the tacit "
                            "knowledge that never lands in the diff."
                        ),
                        "properties": {
                            "topic": {
                                "type": "string",
                                "description": (
                                    "A SHORT human-readable knowledge NAME — a noun phrase "
                                    "WITH SPACES (e.g. 'Idempotent webhooks'), NOT a kebab-case "
                                    "or snake_case slug, a task sentence, or a file path."
                                ),
                            },
                            "insight": {
                                "type": "string",
                                "description": "What to remember and WHY it matters (1-3 sentences).",
                            },
                        },
                    },
                },
                "required": ["checks"],
            },
            handler=self._declare_verification,
        )

    def _require_declared_contract(self, tool: str) -> None:
        """B7 — refuse a mutating file tool until ``declare_verification`` has
        been called at least once this run. Declaring latches ``declared_contract``;
        once set, every later write/edit passes. Raises :class:`ToolError` with an
        actionable message naming the unlock tool."""
        if self.declared_contract is None:
            raise ToolError(f"{tool}: {VERIFY_FIRST_REFUSAL}")

    def _resolve(self, raw: str) -> Path:
        candidate = (self._root / raw).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise ToolError(f"Path {raw!r} escapes the workspace") from exc
        return candidate

    async def _file_read(self, args: dict[str, Any]) -> str:
        raw_path = str(args.get("path") or "")
        if not raw_path:
            raise ToolError("file_read requires 'path'")
        if self._sandbox is not None:
            try:
                data = await self._sandbox.read_file(raw_path, FILE_READ_MAX_BYTES)
            except SandboxError as exc:
                raise ToolError(f"file_read: {exc}") from exc
            text = data.decode("utf-8", errors="replace")
            if len(data) >= FILE_READ_MAX_BYTES:
                text += f"\n... (truncated at {FILE_READ_MAX_BYTES} bytes)"
            self._grounded_paths.add(os.path.normpath(raw_path))
            return text
        target = self._resolve(raw_path)
        if not target.exists():
            raise ToolError(f"file_read: not found: {raw_path}")
        if not target.is_file():
            raise ToolError(f"file_read: not a file: {raw_path}")
        result = await asyncio.to_thread(_read_text_capped, target, FILE_READ_MAX_BYTES)
        self._grounded_paths.add(os.path.normpath(raw_path))
        return result

    async def _file_list(self, args: dict[str, Any]) -> str:
        raw_path = str(args.get("path") or ".")
        if self._sandbox is not None:
            try:
                entries = await self._sandbox.list_dir(raw_path)
            except SandboxError as exc:
                raise ToolError(f"file_list: {exc}") from exc
            return "\n".join(entries) if entries else "(empty)"
        target = self._resolve(raw_path)
        if not target.exists():
            raise ToolError(f"file_list: not found: {raw_path}")
        if not target.is_dir():
            raise ToolError(f"file_list: not a directory: {raw_path}")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        return "\n".join(entries) if entries else "(empty)"

    async def _file_write(self, args: dict[str, Any]) -> str:
        self._require_declared_contract("file_write")
        raw_path = str(args.get("path") or "")
        if not raw_path:
            raise ToolError("file_write requires 'path'")
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("file_write requires string 'content'")
        encoded = content.encode("utf-8")
        if len(encoded) > FILE_WRITE_MAX_BYTES:
            raise ToolError(f"file_write: content exceeds {FILE_WRITE_MAX_BYTES} bytes")
        if self._sandbox is not None:
            try:
                await self._sandbox.write_file(raw_path, encoded)
            except SandboxError as exc:
                raise ToolError(f"file_write: {exc}") from exc
            self._grounded_paths.add(os.path.normpath(raw_path))
            return f"wrote {raw_path} ({len(content)} chars)"
        target = self._resolve(raw_path)
        await asyncio.to_thread(_write_text, target, content)
        self._grounded_paths.add(os.path.normpath(raw_path))
        return f"wrote {raw_path} ({len(content)} chars)"

    async def _read_for_edit(self, raw_path: str) -> str:
        """Read a file's full current content for ``file_edit``. Refuses
        a file larger than the write cap — editing a truncated view
        would corrupt the tail on write-back."""
        if self._sandbox is not None:
            try:
                data = await self._sandbox.read_file(raw_path, FILE_WRITE_MAX_BYTES + 1)
            except SandboxError as exc:
                raise ToolError(f"file_edit: {exc}") from exc
        else:
            target = self._resolve(raw_path)
            if not target.is_file():
                raise ToolError(f"file_edit: not found: {raw_path}")
            data = await asyncio.to_thread(target.read_bytes)
        if len(data) > FILE_WRITE_MAX_BYTES:
            raise ToolError(f"file_edit: {raw_path} is too large to edit safely")
        return data.decode("utf-8", errors="replace")

    async def _file_edit(self, args: dict[str, Any]) -> str:
        self._require_declared_contract("file_edit")
        raw_path = str(args.get("path") or "")
        if not raw_path:
            raise ToolError("file_edit requires 'path'")
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if not isinstance(old_string, str) or old_string == "":
            raise ToolError("file_edit requires a non-empty string 'old_string'")
        if not isinstance(new_string, str):
            raise ToolError("file_edit requires a string 'new_string'")
        if old_string == new_string:
            raise ToolError(
                "file_edit: old_string and new_string are identical — nothing to change"
            )
        replace_all = bool(args.get("replace_all", False))
        if os.path.normpath(raw_path) not in self._grounded_paths:
            raise ToolError(
                f"file_edit: file_read {raw_path} before editing it — "
                "old_string must match the file's actual content"
            )
        content = await self._read_for_edit(raw_path)
        occurrences = content.count(old_string)
        if occurrences == 0:
            raise ToolError(f"file_edit: old_string not found in {raw_path}")
        if occurrences > 1 and not replace_all:
            raise ToolError(
                f"file_edit: old_string occurs {occurrences}× in {raw_path} — add surrounding "
                "context to make it unique, or set replace_all=true"
            )
        updated = (
            content.replace(old_string, new_string)
            if replace_all
            else content.replace(old_string, new_string, 1)
        )
        encoded = updated.encode("utf-8")
        if len(encoded) > FILE_WRITE_MAX_BYTES:
            raise ToolError(f"file_edit: result exceeds {FILE_WRITE_MAX_BYTES} bytes")
        if self._sandbox is not None:
            try:
                await self._sandbox.write_file(raw_path, encoded)
            except SandboxError as exc:
                raise ToolError(f"file_edit: {exc}") from exc
        else:
            await asyncio.to_thread(_write_text, self._resolve(raw_path), updated)
        count = occurrences if replace_all else 1
        return f"edited {raw_path} ({count} replacement{'s' if count != 1 else ''})"

    async def _declare_verification(self, args: dict[str, Any]) -> str:
        # Imported here to keep the tools module free of a core import
        # cycle (verification_contract is pure, but the import site is
        # kept local for symmetry with the rest of the registry).
        from backend.workflow.domain.verifier_contract import (
            parse_verification_contract,  # noqa: PLC0415
        )

        checks = args.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ToolError("declare_verification requires a non-empty 'checks' array")
        contract = parse_verification_contract({"checks": checks})
        if contract is None:
            raise ToolError(
                "declare_verification: no usable check. A 'command' check needs a "
                "non-empty 'command'; a 'judge' check needs a non-empty 'criteria' list."
            )
        self.declared_contract = contract.to_dict()
        # v2 — capture the agent's retrospective knowledge declaration (if any).
        # ``parse_declared_knowledge`` reads ``args["knowledge"]`` and is biased
        # to None (no block / blank → routine work leaves no note). A re-declared
        # contract that omits the block does NOT clear an earlier declaration.
        from backend.knowledge.extraction.worth_remembering import (  # noqa: PLC0415
            parse_declared_knowledge,
        )

        declared = parse_declared_knowledge(args)
        if declared is not None:
            self.declared_knowledge = declared
        n_cmd = len(contract.command_checks)
        n_judge = len(contract.judge_checks)
        return (
            f"verification contract recorded: {n_cmd} command check(s), "
            f"{n_judge} judge check(s). Now write the tests, then implement."
        )

    async def _shell_exec(self, args: dict[str, Any]) -> str:
        command = str(args.get("command") or "")
        if not command.strip():
            raise ToolError("shell_exec requires non-empty 'command'")
        normalized = " " + command.strip() + " "
        for pattern in SHELL_DENYLIST_PATTERNS:
            if pattern in normalized:
                raise ToolError(f"shell_exec: refused by denylist: {pattern.strip()!r}")
        if self._sandbox is not None:
            result = await self._sandbox.exec(command, timeout_s=SHELL_TIMEOUT_S, shell=True)
            if result.timed_out:
                raise ToolError(f"shell_exec: timed out after {SHELL_TIMEOUT_S}s")
            output = "\n".join(chunk for chunk in (result.stdout, result.stderr) if chunk)
            return f"exit={result.exit_code}\n{output[-4000:]}"
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            raise ToolError(f"shell_exec: bad shell syntax: {exc}") from exc
        if not parts:
            raise ToolError("shell_exec: empty command")
        try:
            process = await asyncio.create_subprocess_exec(
                *parts,
                cwd=str(self._root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return f"command not found: {exc}"
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=SHELL_TIMEOUT_S)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise ToolError(f"shell_exec: timed out after {SHELL_TIMEOUT_S}s") from None
        output = "\n".join(
            chunk.decode("utf-8", errors="replace") for chunk in (stdout, stderr) if chunk
        )
        return f"exit={process.returncode}\n{output[-4000:]}"


def _read_text_capped(path: Path, cap: int) -> str:
    data = path.read_bytes()
    if len(data) > cap:
        return data[:cap].decode("utf-8", errors="replace") + f"\n... (truncated at {cap} bytes)"
    return data.decode("utf-8", errors="replace")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


__all__ = [
    "FILE_READ_MAX_BYTES",
    "FILE_WRITE_MAX_BYTES",
    "SHELL_DENYLIST_PATTERNS",
    "SHELL_TIMEOUT_S",
    "ToolDefinition",
    "ToolError",
    "ToolRegistry",
    "VERIFY_FIRST_REFUSAL",
]
