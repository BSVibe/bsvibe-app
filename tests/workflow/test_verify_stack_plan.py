"""How a product stands a disposable instance of itself up — derived, not assumed.

"One instance" means different things per product. bsvibe-app is
``docker compose up``; BStockReport is a CLI, so it has **no stack at all** —
its repo *is* the environment. So the plan is derived from what the repo
actually declares (the same instinct as the derived gate reading manifests),
with product metadata able to override for the odd case.

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


def test_a_repo_with_no_compose_has_no_stack() -> None:
    """A CLI product (BStockReport) is not broken — it simply has no stack to
    stand up. ``None`` is the honest answer, not an error."""
    assert derive_stack_plan(repo_files=["pyproject.toml", "README.md"], project="p") is None


def test_compose_plus_overlay_derives_up_and_down() -> None:
    plan = derive_stack_plan(repo_files=_COMPOSE, project="verify-slot-0")

    assert plan is not None
    assert plan.source == "compose"
    for cmd in (plan.up, plan.down):
        assert "-p verify-slot-0" in cmd, "the stack must live under the SLOT's project"
        assert "deploy/compose.yaml" in cmd
        assert "deploy/compose.verify.yaml" in cmd, "isolation overlay is not optional"
    assert " up " in f" {plan.up} "
    assert "down" in plan.down
    assert "-v" in plan.down, "volumes must go too — the disk is the bound"


def test_compose_without_the_isolation_overlay_is_refused() -> None:
    """Booting the base compose again on this host collides with prod's fixed
    container_name / ports / shared image tag. Refuse loudly; do NOT fall back
    to an unisolated stand-up."""
    with pytest.raises(StackPlanError, match="isolation overlay"):
        derive_stack_plan(repo_files=["deploy/compose.yaml", "pyproject.toml"], project="p")


def test_metadata_overrides_the_derivation() -> None:
    plan = derive_stack_plan(
        repo_files=_COMPOSE,
        project="verify-slot-1",
        metadata={"verify_stack": {"up": "make up P={project}", "down": "make down P={project}"}},
    )

    assert plan is not None
    assert plan.source == "metadata"
    assert plan.up == "make up P=verify-slot-1"
    assert plan.down == "make down P=verify-slot-1"


def test_metadata_override_needs_both_halves() -> None:
    """A stand-up with no matching tear-down leaks a stack forever. Half a
    declaration is a misconfiguration, not a partial feature."""
    with pytest.raises(StackPlanError, match="both"):
        derive_stack_plan(
            repo_files=_COMPOSE, project="p", metadata={"verify_stack": {"up": "make up"}}
        )


def test_metadata_can_declare_that_there_is_no_stack() -> None:
    """An explicit opt-out beats guessing from files — a repo may ship a compose
    file that is not how its verification instance comes up."""
    plan = derive_stack_plan(repo_files=_COMPOSE, project="p", metadata={"verify_stack": None})
    assert plan is None


def test_boot_timeout_is_its_own_budget() -> None:
    """A cold image build is minutes; charging it to the per-command gate budget
    (900s) would turn a slow build into a false verification failure."""
    from backend.config import get_settings

    settings = get_settings()
    assert settings.verify_stack_boot_timeout_s > settings.verify_gate_command_timeout_s
