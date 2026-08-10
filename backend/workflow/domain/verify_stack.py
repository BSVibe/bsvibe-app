"""How a product stands a DISPOSABLE instance of itself up, and tears it down.

Full-surface verification drives a change through the product's real user
surfaces before it merges — which first requires an instance of the product to
drive. "An instance" means different things per product:

* ``bsvibe-app`` is a stack: ``docker compose up`` over postgres/redis/backend/
  worker/pwa.
* ``BStockReport`` is a CLI. It has **no stack at all** — its repo *is* the
  environment. That is not a gap; it is the truth about that product.

So the plan is DERIVED from what the repo actually declares — the same instinct
as the derived verification gate reading the repo's manifests — with product
metadata able to override when a repo's files do not tell the whole story.

Derivation is deterministic here, deliberately: "does this repo have a compose
file" is a fact, not a judgement. The LLM's job is authoring CHECKS, not
guessing how to boot.

⚠️ **The safety property this module exists to hold.** The disposable stack
comes up on the SAME machine that runs production. ``deploy/compose.yaml``
pins a fixed ``container_name``, fixed host ports, and a shared image tag, so a
second stack without ``deploy/compose.verify.yaml`` fights prod for all three —
a collision that has already taken this stack down once. Therefore a compose
file WITHOUT the isolation overlay is refused, loudly. There is no fallback to
an unisolated stand-up: not verifying beats killing production.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The repo-relative compose files that make a stand-up both possible and SAFE.
#: Both must be present — see the module docstring.
_COMPOSE_BASE = "deploy/compose.yaml"
_COMPOSE_VERIFY = "deploy/compose.verify.yaml"

#: Product-metadata key carrying an explicit override. ``None`` under this key
#: is an explicit "this product has no verification stack" — louder and more
#: trustworthy than inferring absence from a file listing.
_METADATA_KEY = "verify_stack"


class StackPlanError(ValueError):
    """The declared / derived stand-up cannot be used safely.

    Raised rather than degraded: every alternative here (booting without the
    isolation overlay, standing up with no way to tear down) trades a missing
    verification for a broken production or a leaked stack.
    """


@dataclass(frozen=True)
class StackPlan:
    """Shell commands that bring one disposable instance up and take it down."""

    up: str
    down: str
    #: ``"compose"`` (derived from the repo) or ``"metadata"`` (product override).
    source: str


def _from_metadata(raw: Any, project: str) -> StackPlan | None:
    if not isinstance(raw, Mapping):
        raise StackPlanError(
            f"{_METADATA_KEY} must be a mapping with 'up' and 'down' (got {type(raw).__name__})"
        )
    up = str(raw.get("up") or "").strip()
    down = str(raw.get("down") or "").strip()
    if not up or not down:
        # A stand-up with no matching tear-down leaks a stack for good; a
        # tear-down with no stand-up never runs. Half a declaration is a
        # misconfiguration, not a partial feature.
        raise StackPlanError(f"{_METADATA_KEY} needs both 'up' and 'down'")
    return StackPlan(
        up=up.format(project=project), down=down.format(project=project), source="metadata"
    )


def derive_stack_plan(
    *,
    repo_files: Sequence[str],
    project: str,
    metadata: Mapping[str, Any] | None = None,
) -> StackPlan | None:
    """The stand-up / tear-down plan for one disposable instance, or ``None``.

    ``None`` means this product legitimately has no stack — its repo is the
    environment (a CLI, a library). That is an honest answer, not a failure.

    ``project`` is the compose project name, which the caller takes from the
    held verification SLOT: naming the stack after the slot is what makes the
    next acquirer clean up a dead holder's leftovers (see
    :mod:`backend.workflow.infrastructure.verify_slots`).
    """
    if metadata is not None and _METADATA_KEY in metadata:
        raw = metadata[_METADATA_KEY]
        if raw is None:
            return None
        return _from_metadata(raw, project)

    files = set(repo_files)
    if _COMPOSE_BASE not in files:
        return None
    if _COMPOSE_VERIFY not in files:
        raise StackPlanError(
            f"{_COMPOSE_BASE} is present but the isolation overlay {_COMPOSE_VERIFY} is not — "
            "standing the base stack up on this host would collide with production's "
            "container_name / ports / image tag. Refusing to boot unisolated."
        )
    compose = f"docker compose -p {project} -f {_COMPOSE_BASE} -f {_COMPOSE_VERIFY}"
    return StackPlan(
        up=f"{compose} up -d --wait",
        # ``-v`` because the volumes are the disk cost, and a full disk on the
        # founder's machine is unrecoverable.
        down=f"{compose} down -v",
        source="compose",
    )


__all__ = ["StackPlan", "StackPlanError", "derive_stack_plan"]
