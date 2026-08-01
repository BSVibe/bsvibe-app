"""``bsvibe-worker service`` — GitHub-Actions-runner-style durability.

The executor worker is user-run and self-hosted (like a GitHub Actions runner);
BSVibe provides the tooling, the user keeps it alive. This renders the OS service
unit from the already-saved register config and installs it with auto-restart —
launchd ``KeepAlive`` on macOS, systemd ``Restart=`` on Linux — replacing the old
manual ``{PLACEHOLDER}``-editing example templates.

SECURITY: the generated unit carries NO worker token. ``bsvibe-worker run`` reads
``~/.bsvibe/worker.token`` (mode 0600) itself, so nothing plaintext is written
into the (historically world-readable) plist — closing that audit finding by
design. The unit file itself is written mode 0600.

The render + plan functions are pure; ``install/uninstall`` take an injected
``run`` callable so the ``launchctl``/``systemctl`` calls are testable.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from backend.executors.worker.credentials import WorkerConfig

SERVICE_LABEL = "com.bsvibe.worker"
_SYSTEMD_UNIT = "bsvibe-worker.service"

Runner = Callable[[list[str]], object]


class ServiceError(Exception):
    """Raised when service install/uninstall cannot proceed."""


@dataclass(frozen=True)
class ServicePlan:
    """Everything needed to (un)install the worker service on one platform."""

    platform: str
    unit_path: Path
    unit_content: str
    unit_mode: int
    load_cmds: list[list[str]]
    unload_cmds: list[list[str]]
    status_cmds: list[list[str]]


def _worker_env(config: WorkerConfig, *, exec_path: str) -> dict[str, str]:
    # No token here — the worker reads ~/.bsvibe/worker.token itself. PATH carries
    # the venv + Homebrew (docker) since service managers start with a minimal PATH.
    venv_bin = str(Path(exec_path).parent)
    return {
        "BSVIBE_WORKER_SERVER_URL": config.server_url,
        "BSVIBE_WORKER_NAME": config.name,
        "PATH": f"{venv_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }


def render_launchd_plist(*, exec_path: str, workdir: str, log_dir: str, env: dict[str, str]) -> str:
    env_entries = "".join(
        f"        <key>{escape(k)}</key>\n        <string>{escape(v)}</string>\n"
        for k, v in env.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"    <key>Label</key>\n    <string>{SERVICE_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        f"    <array>\n        <string>{escape(exec_path)}</string>\n"
        "        <string>run</string>\n    </array>\n"
        f"    <key>WorkingDirectory</key>\n    <string>{escape(workdir)}</string>\n"
        f"    <key>EnvironmentVariables</key>\n    <dict>\n{env_entries}    </dict>\n"
        "    <key>RunAtLoad</key>\n    <true/>\n"
        "    <key>KeepAlive</key>\n"
        "    <dict>\n        <key>SuccessfulExit</key>\n        <false/>\n"
        "        <key>Crashed</key>\n        <true/>\n    </dict>\n"
        "    <key>ThrottleInterval</key>\n    <integer>10</integer>\n"
        f"    <key>StandardOutPath</key>\n    <string>{escape(log_dir)}/bsvibe-worker.out.log</string>\n"
        f"    <key>StandardErrorPath</key>\n    <string>{escape(log_dir)}/bsvibe-worker.err.log</string>\n"
        "</dict>\n</plist>\n"
    )


def render_systemd_unit(*, exec_path: str, workdir: str, env: dict[str, str]) -> str:
    env_lines = "".join(f"Environment={k}={v}\n" for k, v in env.items())
    return (
        "[Unit]\n"
        "Description=BSVibe Executor Worker\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={workdir}\n"
        f"{env_lines}"
        f"ExecStart={exec_path} run\n"
        "Restart=always\n"
        "RestartSec=10\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def build_plan(
    config: WorkerConfig,
    *,
    platform: str,
    home: Path,
    exec_path: str,
    repo: str,
) -> ServicePlan:
    env = _worker_env(config, exec_path=exec_path)
    if platform == "darwin":
        uid = os.getuid()
        domain = f"gui/{uid}"
        unit_path = home / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        content = render_launchd_plist(
            exec_path=exec_path,
            workdir=repo,
            log_dir=str(home / "Library" / "Logs"),
            env=env,
        )
        return ServicePlan(
            platform="darwin",
            unit_path=unit_path,
            unit_content=content,
            unit_mode=0o600,
            load_cmds=[
                ["launchctl", "bootstrap", domain, str(unit_path)],
                ["launchctl", "kickstart", "-k", f"{domain}/{SERVICE_LABEL}"],
            ],
            unload_cmds=[["launchctl", "bootout", f"{domain}/{SERVICE_LABEL}"]],
            status_cmds=[["launchctl", "print", f"{domain}/{SERVICE_LABEL}"]],
        )
    if platform == "linux":
        unit_path = home / ".config" / "systemd" / "user" / _SYSTEMD_UNIT
        content = render_systemd_unit(exec_path=exec_path, workdir=repo, env=env)
        return ServicePlan(
            platform="linux",
            unit_path=unit_path,
            unit_content=content,
            unit_mode=0o600,
            load_cmds=[
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", _SYSTEMD_UNIT],
            ],
            unload_cmds=[["systemctl", "--user", "disable", "--now", _SYSTEMD_UNIT]],
            status_cmds=[["systemctl", "--user", "status", _SYSTEMD_UNIT]],
        )
    raise ServiceError(f"unsupported platform for service install: {platform}")


def install_service(plan: ServicePlan, *, run: Runner) -> None:
    """Write the unit file (mode 0600) then run the load commands in order."""
    plan.unit_path.parent.mkdir(parents=True, exist_ok=True)
    plan.unit_path.write_text(plan.unit_content)
    os.chmod(plan.unit_path, plan.unit_mode)
    for cmd in plan.load_cmds:
        run(cmd)


def uninstall_service(plan: ServicePlan, *, run: Runner) -> None:
    """Run the unload commands (best-effort) then remove the unit file."""
    for cmd in plan.unload_cmds:
        run(cmd)
    plan.unit_path.unlink(missing_ok=True)


def service_status(plan: ServicePlan, *, run: Runner) -> None:
    for cmd in plan.status_cmds:
        run(cmd)


__all__ = [
    "SERVICE_LABEL",
    "ServiceError",
    "ServicePlan",
    "build_plan",
    "install_service",
    "render_launchd_plist",
    "render_systemd_unit",
    "service_status",
    "uninstall_service",
]
