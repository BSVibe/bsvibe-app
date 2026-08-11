"""The compose stand-up, run against a REAL docker daemon and a real stack.

The sibling unit test compares strings, and a string cannot answer the question
this lift exists for: **can a check actually reach the stack it just stood up?**
The isolation overlay publishes no host ports on purpose, so "the plan looks
right" and "the probe can talk to the product" are genuinely different claims.

Four properties, and the first two are a matched pair — each is worthless alone:

1. the prober REACHES a service over the compose network, by its service name;
2. the HOST cannot, because the overlay reset the published port. Without this
   half, (1) could pass on a stack that was published to the host all along —
   which is the collision that has already taken production down once;
3. the run's source arrives in the prober, so a probe suite from the repo can run;
4. teardown leaves neither the prober nor the NETWORK behind. Measured
   2026-08-11: ``docker compose down -v`` cannot remove a network with an
   endpoint still attached — it says "Resource is still in use" and exits **0**.
   A leak that reports success is the exact failure mode this track removes.

Skipped without docker + compose; CI runners have both. The ambient docker
context is used deliberately — this proves the command mechanics, while WHICH
daemon a real verification talks to is pinned by the caller
(``verification_stack._pinned``).
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from backend.workflow.domain.verify_stack import derive_stack_plan

#: ~4MB, and it carries both halves of the exchange: ``httpd`` serves, ``wget``
#: fetches, ``tar`` receives the source copy. NOT ``alpine`` — Alpine moved the
#: ``httpd`` applet out of its busybox build, so the service exits 127 there.
#: The toolchain→image derivation is covered as a unit; pulling a full python
#: image per CI run would buy nothing here.
_IMAGE = "busybox:1.36"

_PROJECT = "bsvibe-verify-compose-selftest"
_TIMEOUT_S = 300

#: Published by the base compose and reset by the overlay. High and odd so a
#: bound port means our stack published it, not that something else was there.
_HOST_PORT = 18099
_SERVICE_PORT = 8080


def _tooling_missing() -> bool:
    if shutil.which("docker") is None:
        return True
    for argv in (["docker", "info"], ["docker", "compose", "version"]):
        probe = subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv,
            capture_output=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
        if probe.returncode != 0:
            return True
    return False


pytestmark = pytest.mark.skipif(_tooling_missing(), reason="no docker + compose on this host")


def _run(command: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S602 — the command under test IS a shell string
        command,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )


def _host_can_connect(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
def compose_repo(tmp_path: Path) -> Path:
    """A repo shaped like a compose product: a base that publishes a host port,
    and the isolation overlay that takes it away.

    The published port in the base is not decoration — it is what makes the
    "host cannot reach it" assertion mean something. bsvibe-app's real
    ``compose.yaml`` publishes four.
    """
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "compose.yaml").write_text(
        "services:\n"
        "  svc:\n"
        f"    image: {_IMAGE}\n"
        "    command:\n"
        '      - "sh"\n'
        '      - "-c"\n'
        '      - "mkdir -p /www && echo alive > /www/index.html && '
        f'httpd -f -p {_SERVICE_PORT} -h /www"\n'
        "    ports:\n"
        f'      - "{_HOST_PORT}:{_SERVICE_PORT}"\n'
    )
    (tmp_path / "deploy" / "compose.verify.yaml").write_text(
        "services:\n  svc:\n    ports: !reset []\n"
    )
    (tmp_path / "surface_probe.sh").write_text("from-the-repo\n")
    (tmp_path / ".env").write_text("ALPACA_API_KEY=PK-real-secret\n")
    return tmp_path


def test_the_prober_reaches_the_stack_that_the_host_cannot(compose_repo: Path) -> None:
    plan = derive_stack_plan(
        repo_files=["deploy/compose.yaml", "deploy/compose.verify.yaml"],
        project=_PROJECT,
        workspace_path=str(compose_repo),
        metadata={"verify_stack": {"image": _IMAGE}},
    )
    assert plan is not None
    assert plan.source == "compose"

    # The compose commands are repo-relative, exactly as the box runs them.
    _run(plan.down, cwd=compose_repo)  # idempotent; clears an interrupted run
    try:
        up = _run(plan.up, cwd=compose_repo)
        assert up.returncode == 0, f"stand-up failed: {up.stdout}\n{up.stderr}"

        reached = _run(plan.wrap(f"wget -qO- http://svc:{_SERVICE_PORT}/"), cwd=compose_repo)
        assert reached.returncode == 0, (
            f"the prober could not reach the stack over its network: {reached.stderr}"
        )
        assert reached.stdout.strip() == "alive", (
            "a surface probe has to get the SERVICE's answer, not merely resolve a name"
        )

        assert not _host_can_connect(_HOST_PORT), (
            f"the overlay did not take the published port away — port {_HOST_PORT} is bound on "
            "the host, which is the collision vector that has taken production down before"
        )

        got = _run(plan.wrap("cat surface_probe.sh"), cwd=compose_repo)
        assert got.returncode == 0 and got.stdout.strip() == "from-the-repo", (
            "the run's source must reach the prober — a probe suite lives in the repo"
        )
        secrets = _run(plan.wrap("cat .env"), cwd=compose_repo)
        assert secrets.returncode != 0, "the founder's real .env travelled into the prober"
    finally:
        down = _run(plan.down, cwd=compose_repo)
        assert down.returncode == 0, f"teardown failed: {down.stderr}"

    left = _run(
        f"docker ps -a --filter name={_PROJECT} --format '{{{{.Names}}}}'", cwd=compose_repo
    )
    assert left.stdout.strip() == "", f"teardown left containers behind: {left.stdout!r}"

    networks = _run(
        f"docker network ls --filter label=com.docker.compose.project={_PROJECT} "
        "--format '{{.Name}}'",
        cwd=compose_repo,
    )
    assert networks.stdout.strip() == "", (
        "teardown leaked the network — `compose down -v` cannot remove one with an endpoint "
        f"still attached and exits 0 while saying so: {networks.stdout!r}"
    )
