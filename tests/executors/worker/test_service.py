"""``bsvibe-worker service install|uninstall|status`` — GitHub-runner-style svc.

The worker is a user-run, self-hosted executor (like a GitHub Actions runner).
This makes it DURABLE with the OS service manager (launchd KeepAlive / systemd
Restart) via one command, rendering the unit from the already-saved register
config. The generated unit carries NO worker token — ``bsvibe-worker run`` reads
``~/.bsvibe/worker.token`` itself, so nothing plaintext lands in the (previously
world-readable) plist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.executors.worker.credentials import WorkerConfig
from backend.executors.worker.service import (
    SERVICE_LABEL,
    ServiceError,
    build_plan,
    install_service,
    render_launchd_plist,
    render_systemd_unit,
    uninstall_service,
)


def _config() -> WorkerConfig:
    return WorkerConfig(
        name="mac-mini",
        capabilities=["claude_code"],
        labels=[],
        server_url="https://api.bsvibe.dev",
        saved_at=0,
    )


# --------------------------------------------------------------------------- #
# render — pure
# --------------------------------------------------------------------------- #
def test_render_launchd_plist_has_keepalive_and_no_token() -> None:
    xml = render_launchd_plist(
        exec_path="/repo/.venv/bin/bsvibe-worker",
        workdir="/repo",
        log_dir="/Users/x/Library/Logs",
        env={
            "BSVIBE_WORKER_SERVER_URL": "https://api.bsvibe.dev",
            "BSVIBE_WORKER_NAME": "mac-mini",
        },
    )
    assert f"<string>{SERVICE_LABEL}</string>" in xml
    assert "<string>/repo/.venv/bin/bsvibe-worker</string>" in xml
    assert "<string>run</string>" in xml  # runs the long-poll loop
    assert "<key>KeepAlive</key>" in xml and "<key>RunAtLoad</key>" in xml
    assert "/Users/x/Library/Logs/bsvibe-worker.out.log" in xml
    assert "https://api.bsvibe.dev" in xml and "mac-mini" in xml
    # SECURITY: never emit the worker token into the plist.
    assert "BSVIBE_WORKER_TOKEN" not in xml
    assert "{" not in xml and "}" not in xml or "<dict>" in xml  # no leftover {PLACEHOLDER}
    assert "{USER}" not in xml and "{REPO}" not in xml and "{SERVER_URL}" not in xml


def test_render_systemd_unit_restarts_and_no_token() -> None:
    unit = render_systemd_unit(
        exec_path="/home/x/.venv/bin/bsvibe-worker",
        workdir="/home/x/repo",
        env={"BSVIBE_WORKER_SERVER_URL": "https://api.bsvibe.dev", "BSVIBE_WORKER_NAME": "srv"},
    )
    assert "ExecStart=/home/x/.venv/bin/bsvibe-worker run" in unit
    assert "Restart=" in unit and "WantedBy=" in unit
    assert "WorkingDirectory=/home/x/repo" in unit
    assert "Environment=BSVIBE_WORKER_SERVER_URL=https://api.bsvibe.dev" in unit
    assert "BSVIBE_WORKER_TOKEN" not in unit
    assert "{REPO}" not in unit and "{USER}" not in unit


# --------------------------------------------------------------------------- #
# build_plan
# --------------------------------------------------------------------------- #
def test_build_plan_darwin(tmp_path: Path) -> None:
    plan = build_plan(
        _config(),
        platform="darwin",
        home=tmp_path,
        exec_path="/repo/.venv/bin/bsvibe-worker",
        repo="/repo",
    )
    assert plan.platform == "darwin"
    assert plan.unit_path == tmp_path / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    assert plan.unit_mode == 0o600
    assert "<key>KeepAlive</key>" in plan.unit_content
    # launchd install/uninstall use bootstrap/bootout on the gui domain.
    assert any("bootstrap" in " ".join(c) for c in plan.load_cmds)
    assert any("bootout" in " ".join(c) for c in plan.unload_cmds)


def test_build_plan_linux(tmp_path: Path) -> None:
    plan = build_plan(
        _config(),
        platform="linux",
        home=tmp_path,
        exec_path="/home/x/.venv/bin/bsvibe-worker",
        repo="/home/x/repo",
    )
    assert plan.platform == "linux"
    assert plan.unit_path.name == "bsvibe-worker.service"
    assert "Restart=" in plan.unit_content
    assert any("daemon-reload" in " ".join(c) for c in plan.load_cmds)
    assert any("enable" in " ".join(c) or "start" in " ".join(c) for c in plan.load_cmds)


def test_build_plan_rejects_unknown_platform(tmp_path: Path) -> None:
    with pytest.raises(ServiceError, match="unsupported"):
        build_plan(_config(), platform="win32", home=tmp_path, exec_path="x", repo="/r")


# --------------------------------------------------------------------------- #
# install_service — writes the unit (0600) + runs load cmds via injected runner
# --------------------------------------------------------------------------- #
def test_install_writes_unit_0600_and_runs_load(tmp_path: Path) -> None:
    plan = build_plan(
        _config(),
        platform="darwin",
        home=tmp_path,
        exec_path="/repo/.venv/bin/bsvibe-worker",
        repo="/repo",
    )
    ran: list[list[str]] = []
    install_service(plan, run=lambda cmd: ran.append(cmd))
    assert plan.unit_path.exists()
    assert (plan.unit_path.stat().st_mode & 0o777) == 0o600
    assert plan.unit_path.read_text() == plan.unit_content
    assert ran == plan.load_cmds  # load commands ran in order


def test_uninstall_runs_unload_and_removes_unit(tmp_path: Path) -> None:
    plan = build_plan(
        _config(),
        platform="darwin",
        home=tmp_path,
        exec_path="/repo/.venv/bin/bsvibe-worker",
        repo="/repo",
    )
    plan.unit_path.parent.mkdir(parents=True, exist_ok=True)
    plan.unit_path.write_text(plan.unit_content)
    ran: list[list[str]] = []
    uninstall_service(plan, run=lambda cmd: ran.append(cmd))
    assert not plan.unit_path.exists()
    assert ran == plan.unload_cmds
