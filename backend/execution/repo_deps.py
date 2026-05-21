"""Cloned-repo environment setup for the work + verification sandbox.

A ``github_connected`` project's cloned repo carries a real dependency
tree (``httpx`` via ``uv``; a ``node_modules`` tree via ``pnpm``…). The
Part B sandbox ships only a generic toolchain, so a declared contract
— e.g. ``pytest`` — fails at *collection* with ``ModuleNotFoundError``
when those deps were never installed.

Environment setup is **repo-defined, not infra-guessed**: a repo
declares how to set itself up in `.devcontainer/devcontainer.json`
(the devcontainer standard). :func:`ensure_repo_dependencies` honours
that file's lifecycle commands (`onCreateCommand` /
`updateContentCommand` / `postCreateCommand`). Only when the repo has
no devcontainer does it fall back to a best-effort manifest heuristic
— a stopgap for the first run on a repo that has not had one authored
yet (the work LLM is prompted to add one).

Best-effort and non-fatal: a failed/absent setup is reported via
:class:`InstallResult` and logged — never raised, because a genuine
dependency problem should surface as a failed aspect with a clear
message, not as a verifier crash.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import structlog

from backend.supervisor.sandbox import SandboxSession

logger = structlog.get_logger(__name__)

# Dependency installs pull from the network and can be slow on a cold
# cache — generous, but bounded so a hung registry can't wedge the
# verifier.
DEP_INSTALL_TIMEOUT_S = 300

# devcontainer lifecycle hooks, in execution order. Each may carry a
# setup command; we run whichever are present.
_DEVCONTAINER_HOOKS = ("onCreateCommand", "updateContentCommand", "postCreateCommand")


@dataclass(frozen=True)
class InstallResult:
    """Outcome of :func:`ensure_repo_dependencies`.

    ``status`` is one of:
      - ``skipped``   — no devcontainer and no recognised manifest.
      - ``installed`` — the setup command(s) ran and exited 0.
      - ``failed``    — a setup command ran non-zero, or errored.

    ``source`` is ``devcontainer`` / ``heuristic`` / ``none`` — which
    path produced the command(s).
    """

    status: str
    detail: str | None
    source: str = "none"


def _strip_jsonc(text: str) -> str:
    """devcontainer.json is JSONC — strip ``//`` and ``/* */`` comments
    and trailing commas so ``json.loads`` can parse it."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"(^|[^:])//[^\n]*", lambda m: m.group(1), text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def _devcontainer_path(root: Path) -> Path | None:
    for candidate in (root / ".devcontainer" / "devcontainer.json", root / ".devcontainer.json"):
        if candidate.is_file():
            return candidate
    return None


def _command_strings(value: object) -> list[str]:
    """Normalise a devcontainer lifecycle-command value to shell
    strings. The spec allows a string, an argv array, or an object of
    named commands."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        # An argv array — join into one shell command.
        argv = [str(part) for part in value]
        return [" ".join(argv)] if argv else []
    if isinstance(value, dict):
        out: list[str] = []
        for sub in value.values():
            out.extend(_command_strings(sub))
        return out
    return []


def _devcontainer_setup_commands(root: Path) -> list[str]:
    """Setup commands declared by the repo's devcontainer, in lifecycle
    order. Empty list when there is no devcontainer or it declares no
    lifecycle command."""
    path = _devcontainer_path(root)
    if path is None:
        return []
    try:
        config = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        logger.warning("devcontainer_parse_failed", path=str(path), error=str(exc))
        return []
    if not isinstance(config, dict):
        return []
    commands: list[str] = []
    for hook in _DEVCONTAINER_HOOKS:
        commands.extend(_command_strings(config.get(hook)))
    return commands


def _heuristic_install_command(root: Path) -> str | None:
    """Best-effort fallback for a repo with no devcontainer. Lockfiles
    win over bare manifests so the install is reproducible."""
    if (root / "uv.lock").is_file() or (root / "pyproject.toml").is_file():
        return "uv sync"
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm install --frozen-lockfile"
    if (root / "package-lock.json").is_file():
        return "npm ci"
    if (root / "yarn.lock").is_file():
        return "yarn install --frozen-lockfile"
    if (root / "package.json").is_file():
        return "npm install"
    return None


async def _run_setup(sandbox_session: SandboxSession, command: str) -> tuple[bool, str | None]:
    """Run one setup command. Returns ``(ok, failure_detail)``."""
    try:
        result = await sandbox_session.exec(command, timeout_s=DEP_INSTALL_TIMEOUT_S, shell=True)
    except Exception as exc:  # noqa: BLE001 — best-effort, never abort verification
        logger.warning("repo_setup_errored", command=command, error=str(exc))
        return False, str(exc)
    if result.timed_out:
        logger.warning("repo_setup_timed_out", command=command)
        return False, f"{command} timed out"
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        logger.warning(
            "repo_setup_failed", command=command, exit_code=result.exit_code, detail=detail
        )
        return False, f"{command} exited {result.exit_code}: {detail}"
    return True, None


async def ensure_repo_dependencies(
    *,
    root: Path | str,
    sandbox_session: SandboxSession,
) -> InstallResult:
    """Set the cloned repo's environment up inside ``sandbox_session``.

    Honours the repo's `.devcontainer/devcontainer.json` lifecycle
    commands first (repo-defined); falls back to a manifest heuristic
    only when the repo has no devcontainer. No-op (``skipped``) when
    neither applies. Best-effort — never raises."""
    root = Path(root)

    devcontainer_commands = _devcontainer_setup_commands(root)
    if devcontainer_commands:
        for command in devcontainer_commands:
            ok, detail = await _run_setup(sandbox_session, command)
            if not ok:
                return InstallResult(status="failed", detail=detail, source="devcontainer")
        logger.info("repo_env_ready", source="devcontainer", commands=devcontainer_commands)
        return InstallResult(
            status="installed", detail="; ".join(devcontainer_commands), source="devcontainer"
        )

    heuristic_command = _heuristic_install_command(root)
    if heuristic_command is None:
        return InstallResult(status="skipped", detail="no devcontainer and no dependency manifest")

    ok, detail = await _run_setup(sandbox_session, heuristic_command)
    if not ok:
        return InstallResult(status="failed", detail=detail, source="heuristic")
    logger.info("repo_env_ready", source="heuristic", command=command)
    return InstallResult(status="installed", detail=command, source="heuristic")
