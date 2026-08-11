"""How a product stands a disposable instance of itself up — derived, not assumed.

"One instance" means different things per product. bsvibe-app is
``docker compose up``; BStockReport is a CLI with no compose file — but it still
gets a **container** built from the toolchain its repo declares, because
verification execution has different requirements from agent execution: it must
be REPRODUCIBLE and ISOLATED. Running a CLI product's checks natively on the
founder's machine inherits whatever their ``.venv`` drifted into, can reach
their real ``.env``, and leaves its litter in their tree.

So the plan is derived from what the repo actually declares (the same instinct
as the derived gate reading manifests), with product metadata able to override
for the odd case.

The safety property this module exists to hold: a compose stand-up on this
machine WITHOUT the isolation overlay can kill production — fixed
``container_name``, fixed host ports, a shared image tag (see
``deploy/compose.verify.yaml``). So "compose file present" is not sufficient;
the overlay must be present too, or we refuse rather than boot something that
fights prod for its container names.
"""

from __future__ import annotations

import pytest

from backend.workflow.domain.verify_stack import (
    StackPlanError,
    derive_stack_plan,
)

_COMPOSE = ["deploy/compose.yaml", "deploy/compose.verify.yaml", "pyproject.toml"]
_CLI_REPO = ["pyproject.toml", "uv.lock", "README.md"]


def test_a_repo_with_no_compose_still_gets_a_container() -> None:
    """A CLI product (BStockReport) has no compose file — but "no compose" is
    not "no environment". Its declared toolchain names a container, so its
    checks run reproducibly and isolated instead of on whatever the founder's
    machine happens to have installed today."""
    plan = derive_stack_plan(
        repo_files=_CLI_REPO, project="verify-slot-0", workspace_path="/founder/BStockReport"
    )

    assert plan is not None
    assert plan.source == "container"
    assert "docker run" in plan.up
    assert "verify-slot-0" in plan.up, "the container must be named for the SLOT"
    assert "docker rm -f verify-slot-0" in plan.down, "reclaiming the slot must kill it"
    assert plan.image is not None
    assert "uv" in plan.image, "a uv/pyproject repo names a python toolchain"


def test_container_checks_run_inside_the_container_not_on_the_host() -> None:
    """The whole point. A plan that stands a container up but keeps running the
    commands on the host has isolated nothing."""
    plan = derive_stack_plan(
        repo_files=_CLI_REPO, project="verify-slot-2", workspace_path="/founder/BStockReport"
    )

    assert plan is not None
    wrapped = plan.wrap("uv run pytest -q")
    assert wrapped.startswith("docker exec")
    assert "verify-slot-2" in wrapped
    assert "uv run pytest -q" in wrapped


def test_the_source_is_copied_in_without_the_platform_poisoned_dirs() -> None:
    """``.venv`` holds macOS-native binaries and ``node_modules`` platform
    builds; copying them into a Linux container gives a broken environment that
    LOOKS provisioned. The point of the container is a clean install."""
    plan = derive_stack_plan(
        repo_files=_CLI_REPO, project="p", workspace_path="/founder/BStockReport"
    )

    assert plan is not None
    assert "/founder/BStockReport" in plan.up, "the source must be copied in"
    assert "--exclude=./.venv" in plan.up
    assert "--exclude=./node_modules" in plan.up


def test_the_founders_real_env_file_does_not_travel() -> None:
    """``.env`` is ambient HOST state and the credential surface at once.

    Measured on BStockReport (2026-08-10): its suite fails on the founder's
    machine and inside a container carrying their ``.env`` — a config test reads
    a real Alpaca key where it expects a default — and passes 148/148 without
    it. The same key would let a check place a real order. A product whose
    checks genuinely need host configuration DECLARES that; it must not arrive
    by accident."""
    plan = derive_stack_plan(repo_files=_CLI_REPO, project="p", workspace_path="/ws")

    assert plan is not None
    assert "--exclude=./.env" in plan.up
    assert "'--exclude=*/.env'" in plan.up


def test_a_repo_declaring_no_toolchain_has_no_environment() -> None:
    """Nothing declared means nothing to reproduce. ``None`` here is the honest
    answer — not a container built on a guess."""
    assert derive_stack_plan(repo_files=["README.md"], project="p", workspace_path="/ws") is None


def test_a_node_repo_gets_a_node_container() -> None:
    plan = derive_stack_plan(
        repo_files=["package.json", "pnpm-lock.yaml"], project="p", workspace_path="/ws"
    )

    assert plan is not None
    assert plan.image is not None
    assert "node" in plan.image


def test_metadata_can_override_only_the_image() -> None:
    """The derived image is a default, not a claim about the repo's exact
    interpreter. A product that needs another one says so without having to
    hand-write its whole stand-up."""
    plan = derive_stack_plan(
        repo_files=_CLI_REPO,
        project="p",
        workspace_path="/ws",
        metadata={"verify_stack": {"image": "python:3.13-slim"}},
    )

    assert plan is not None
    assert plan.source == "container"
    assert plan.image == "python:3.13-slim"
    assert "python:3.13-slim" in plan.up


def test_compose_plus_overlay_derives_up_and_down() -> None:
    plan = derive_stack_plan(repo_files=_COMPOSE, project="verify-slot-0", workspace_path="/ws")

    assert plan is not None
    assert plan.source == "compose"
    for cmd in (plan.up, plan.down):
        assert "-p verify-slot-0" in cmd, "the stack must live under the SLOT's project"
        assert "deploy/compose.yaml" in cmd
        assert "deploy/compose.verify.yaml" in cmd, "isolation overlay is not optional"
    assert " up " in f" {plan.up} "
    assert "down" in plan.down
    assert "-v" in plan.down, "volumes must go too — the disk is the bound"


class TestComposeProber:
    """A compose stack has services but no way IN, and that is the whole problem.

    The isolation overlay publishes NO host ports on purpose — that is what keeps
    the disposable stack from fighting production for 5442/6387/8700/3700. The
    consequence is that a command running on the founder's machine cannot reach
    the stack it just stood up: ``localhost:8700`` is prod's, or nothing's.

    So the stack gets a PROBER — a container holding the run's source, attached
    to the stack's own network, where the services answer to their compose
    service names (``backend:8000``, ``postgres:5432``). Same shape the CLI
    products already use; the network is the only thing added.
    """

    def test_a_compose_stack_gets_a_prober_on_its_own_network(self) -> None:
        plan = derive_stack_plan(repo_files=_COMPOSE, project="verify-slot-0", workspace_path="/ws")

        assert plan is not None
        assert plan.source == "compose"
        assert "--network verify-slot-0_default" in plan.up, (
            "without joining the stack's network the prober is just another host process"
        )

    def test_the_checks_run_inside_the_prober(self) -> None:
        """``wrap`` being identity was the bug: it meant "stood a stack up and
        then ran the checks somewhere that cannot see it"."""
        plan = derive_stack_plan(repo_files=_COMPOSE, project="p", workspace_path="/ws")

        assert plan is not None
        wrapped = plan.wrap("uv run pytest tests/surface")
        assert wrapped != "uv run pytest tests/surface"
        assert "docker exec" in wrapped and "p-probe" in wrapped

    def test_the_prober_is_named_after_the_slot(self) -> None:
        """Slot-scoped, like the stack itself: reclaiming a slot has to reclaim a
        dead holder's prober too, and it can only do that if it can name it."""
        plan = derive_stack_plan(repo_files=_COMPOSE, project="verify-slot-3", workspace_path="/ws")

        assert plan is not None
        assert "verify-slot-3-probe" in plan.up
        assert "verify-slot-3-probe" in plan.down

    def test_the_prober_goes_before_the_stack_does(self) -> None:
        """MEASURED, and it exits 0 either way: ``docker compose down -v`` cannot
        remove a network that still has an endpoint attached — it prints
        "Resource is still in use" and returns SUCCESS, leaking one network per
        run. Removing the prober first is the whole fix, so the order is the
        assertion."""
        plan = derive_stack_plan(repo_files=_COMPOSE, project="p", workspace_path="/ws")

        assert plan is not None
        assert plan.down.index("p-probe") < plan.down.index("compose"), (
            f"the stack is torn down before its prober leaves the network: {plan.down!r}"
        )

    def test_the_source_reaches_the_prober(self) -> None:
        """A probe suite is code in the repo; a prober without the source can run
        nothing. Same tar copy as the CLI container — never a bind mount, so
        verification does not write into the tree the agent works in."""
        plan = derive_stack_plan(repo_files=_COMPOSE, project="p", workspace_path="/founder/ws")

        assert plan is not None
        assert "tar -cf - -C /founder/ws" in plan.up
        assert plan.workdir, "callers building absolute paths need to know where the source landed"

    def test_the_founders_env_still_does_not_travel(self) -> None:
        """The prober is a new door into the verification environment; it must
        not become the one that lets ambient host state back in."""
        plan = derive_stack_plan(repo_files=_COMPOSE, project="p", workspace_path="/ws")

        assert plan is not None
        assert "--exclude=./.env" in plan.up

    def test_a_compose_repo_declaring_no_toolchain_has_no_way_in(self) -> None:
        """An honest limit rather than an invented image: nothing declared means
        nothing to reproduce, so the stack stands up and the checks stay where
        they were. No product hits this today (a compose repo without any
        manifest), and guessing an image would be the fabrication the derived
        gate refuses to make."""
        plan = derive_stack_plan(
            repo_files=["deploy/compose.yaml", "deploy/compose.verify.yaml"],
            project="p",
            workspace_path="/ws",
        )

        assert plan is not None
        assert plan.source == "compose"
        assert plan.wrap("echo hi") == "echo hi"

    def test_an_image_override_picks_the_prober_not_a_bare_container(self) -> None:
        """``image`` overrides the IMAGE, not the kind of environment. Dropping
        the compose stack because someone pinned an interpreter would silently
        verify a stackless product."""
        plan = derive_stack_plan(
            repo_files=_COMPOSE,
            project="verify-slot-0",
            workspace_path="/ws",
            metadata={"verify_stack": {"image": "python:3.13-slim"}},
        )

        assert plan is not None
        assert plan.source == "compose", "the repo still declares a stack"
        assert "docker compose" in plan.up
        assert "python:3.13-slim" in plan.up


def test_compose_without_the_isolation_overlay_is_refused() -> None:
    """Booting the base compose again on this host collides with prod's fixed
    container_name / ports / shared image tag. Refuse loudly; do NOT fall back
    to an unisolated stand-up."""
    with pytest.raises(StackPlanError, match="isolation overlay"):
        derive_stack_plan(
            repo_files=["deploy/compose.yaml", "pyproject.toml"], project="p", workspace_path="/ws"
        )


def test_metadata_overrides_the_derivation() -> None:
    plan = derive_stack_plan(
        repo_files=_COMPOSE,
        project="verify-slot-1",
        workspace_path="/ws",
        metadata={"verify_stack": {"up": "make up P={project}", "down": "make down P={project}"}},
    )

    assert plan is not None
    assert plan.source == "metadata"
    assert plan.up == "make up P=verify-slot-1"
    assert plan.down == "make down P=verify-slot-1"


def test_metadata_override_can_say_where_commands_run() -> None:
    """A hand-written stand-up that gives no way in would run its checks on the
    host while claiming an isolated environment — the exact confusion this
    module exists to remove. It is optional, but it is expressible."""
    plan = derive_stack_plan(
        repo_files=_COMPOSE,
        project="verify-slot-1",
        workspace_path="/ws",
        metadata={
            "verify_stack": {
                "up": "make up P={project}",
                "down": "make down P={project}",
                "exec": "docker exec {project} sh -lc {command}",
            }
        },
    )

    assert plan is not None
    assert plan.wrap("pytest -q") == "docker exec verify-slot-1 sh -lc 'pytest -q'"


def test_metadata_override_needs_both_halves() -> None:
    """A stand-up with no matching tear-down leaks a stack forever. Half a
    declaration is a misconfiguration, not a partial feature."""
    with pytest.raises(StackPlanError, match="both"):
        derive_stack_plan(
            repo_files=_COMPOSE,
            project="p",
            workspace_path="/ws",
            metadata={"verify_stack": {"up": "make up"}},
        )


def test_metadata_can_declare_that_checks_run_on_the_host() -> None:
    """Some checks legitimately need HOST resources — BStockReport's ``.env``
    points ``BLOASIS_DB_PATH`` at a SQLite file on the founder's disk, which no
    container sees. Then the honest move is to say so, not to build a container
    that quietly cannot do the job. Mixing the two without declaring which is
    which is the worst of the three."""
    plan = derive_stack_plan(
        repo_files=_COMPOSE, project="p", workspace_path="/ws", metadata={"verify_stack": None}
    )
    assert plan is None


def test_boot_timeout_is_its_own_budget() -> None:
    """A cold image build is minutes; charging it to the per-command gate budget
    (900s) would turn a slow build into a false verification failure."""
    from backend.config import get_settings

    settings = get_settings()
    assert settings.verify_stack_boot_timeout_s > settings.verify_gate_command_timeout_s
