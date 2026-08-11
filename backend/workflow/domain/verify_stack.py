"""How a product stands a DISPOSABLE instance of itself up, and tears it down.

Full-surface verification drives a change through the product's real user
surfaces before it merges — which first requires an instance of the product to
drive. "An instance" means different things per product:

* ``bsvibe-app`` is a stack: ``docker compose up`` over postgres/redis/backend/
  worker/pwa.
* ``BStockReport`` is a CLI with no compose file — but it is NOT environmentless.
  Its repo declares a toolchain (``pyproject.toml`` + ``uv.lock``), and that
  names a **container**.

⭐ **Verification execution is not agent execution.** ``client_attach`` runs the
agent NATIVELY in the founder's own directory on purpose — that is the whole
point of the model. Verification wants the opposite: REPRODUCIBLE and ISOLATED.
Running a CLI product's checks natively inherits whatever that ``.venv`` has
drifted into (``uv.lock`` does not stop a venv drifting), can reach the
founder's real ``.env``, presumes their toolchain, and leaves its litter in
their tree — so a check can pass or fail for reasons that have nothing to do
with the change under test. The container is stood up ON the founder's machine,
so the privacy contract (source never reaches the server) is untouched.

⚠️ **Honest limit — some checks legitimately need HOST resources.**
BStockReport's ``.env`` points ``BLOASIS_DB_PATH`` at a SQLite file on the
founder's disk that no container sees. So this is a matter of DECLARATION, not
of putting everything in a container: ``verify_stack: null`` in product metadata
says "these checks run on the host". Mixing the two without declaring which is
which is worse than either.

So the plan is DERIVED from what the repo actually declares — the same instinct
as the derived verification gate reading the repo's manifests — with product
metadata able to override when a repo's files do not tell the whole story.

Derivation is deterministic here, deliberately: "does this repo have a compose
file", "does it declare a python toolchain" are facts, not judgements. The LLM's
job is authoring CHECKS, not guessing how to boot.

⚠️ **The safety property this module exists to hold.** The disposable stack
comes up on the SAME machine that runs production. ``deploy/compose.yaml``
pins a fixed ``container_name``, fixed host ports, and a shared image tag, so a
second stack without ``deploy/compose.verify.yaml`` fights prod for all three —
a collision that has already taken this stack down once. Therefore a compose
file WITHOUT the isolation overlay is refused, loudly. There is no fallback to
an unisolated stand-up: not verifying beats killing production.

⭐ **And the price of that isolation is that nothing on the host can reach the
stack** — the overlay publishes no ports, by design. So a compose product's
checks run in a PROBER: a container holding the run's source, joined to the
stack's own network, where the services answer to the names they use among
themselves. Without it, standing a stack up and then running the checks on the
host is a verification that cannot see what it claims to verify.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The repo-relative compose files that make a stand-up both possible and SAFE.
#: Both must be present — see the module docstring.
_COMPOSE_BASE = "deploy/compose.yaml"
_COMPOSE_VERIFY = "deploy/compose.verify.yaml"

#: Product-metadata key carrying an explicit override. ``None`` under this key
#: is an explicit "this product's checks run on the host" — louder and more
#: trustworthy than inferring it from a file listing.
_METADATA_KEY = "verify_stack"

#: Where the repo's source is copied to inside a derived container.
_CONTAINER_WORKDIR = "/work"

#: A compose project's auto-created bridge is ``<project>_default`` — where every
#: service without an explicit ``networks:`` key lands, which for bsvibe-app is
#: postgres / redis / pwa, with backend and worker straddling it and
#: ``sandboxnet``. Verified against a real daemon (compose v5.1.1). Joining
#: ``sandboxnet`` is deliberately NOT done: the sandbox control plane is kept off
#: the data services' broadcast domain on purpose, and a prober has no business
#: there.
_COMPOSE_NETWORK_SUFFIX = "_default"

#: The prober's name is the SLOT's, like the stack's own project, so that
#: reclaiming a slot reclaims a dead holder's prober too — it can only remove
#: what it can name.
_PROBE_SUFFIX = "-probe"

#: Manifest → image, in precedence order. Deliberately boring and explicit: the
#: image is DERIVED from a declared toolchain, never guessed from the intent.
#: A repo declaring several toolchains gets the first match — its other
#: toolchain's commands then record ``unavailable`` (exit 127), which is honest
#: but weaker; ``verify_stack.image`` in metadata is the way out.
_TOOLCHAIN_IMAGES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("uv.lock", "pyproject.toml", "requirements.txt"),
        # Carries both CPython and uv, so a repo pinned either way can build.
        "ghcr.io/astral-sh/uv:python3.12-bookworm-slim",
    ),
    (("package.json",), "node:22-bookworm-slim"),
    (("go.mod",), "golang:1.23-bookworm"),
    (("Cargo.toml",), "rust:1-bookworm"),
)

#: Never copied into the container — the two kinds of AMBIENT HOST STATE that
#: decide a check's outcome for reasons having nothing to do with the change.
#:
#: * ``.venv`` / ``node_modules`` hold HOST-platform binaries: a macOS venv
#:   inside a Linux container is an environment that looks provisioned and is
#:   broken.
#: * ``.env`` is the founder's real configuration, and it is auto-loaded by the
#:   conventional toolchains (python-dotenv, pydantic-settings, node). Measured
#:   2026-08-10: BStockReport's suite FAILS on the founder's machine and inside
#:   a container that carries their ``.env`` (a config test reads a real Alpaca
#:   key where it expects a default), and passes 148/148 without it. It is also
#:   the credential surface — a check that reaches a real key can send a real
#:   order or a real message. A product whose checks genuinely need host
#:   configuration declares that (``verify_stack: null``); it must not arrive by
#:   accident. Only the dotenv DEFAULT is filtered — a repo that loads
#:   ``.env.prod`` by name is declaring something else, and ``.env.example`` is
#:   meant to travel.
#:
#: Nested copies (monorepo ``apps/*/node_modules``) are excluded too, so the
#: glob form is required and must stay QUOTED: the copy runs with the workspace
#: as its cwd, where an unquoted ``*/`` would expand.
_UNCOPYABLE = (
    "./.venv",
    "*/.venv",
    "./node_modules",
    "*/node_modules",
    "./.env",
    "*/.env",
)

#: A command template's remaining placeholder after the project is bound.
_COMMAND_SLOT = "{command}"


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
    #: ``"compose"`` / ``"container"`` (derived from the repo) or ``"metadata"``.
    source: str
    #: The image a derived container runs, for the evidence trail: "which
    #: environment did this check actually run in" is part of the claim.
    image: str | None = None
    #: How to run one command INSIDE this environment, with ``{command}`` left
    #: to bind. Empty means "where the box already runs it", which now only
    #: happens for a compose repo declaring no toolchain to build a prober from.
    exec_template: str = ""
    #: Where the source sits INSIDE this environment. Empty when commands run
    #: where the box already runs them. Callers that build absolute paths (the
    #: venv ``PATH`` prefix) need this: a path from the founder's machine means
    #: nothing inside a container, and silently resolves to nothing.
    workdir: str = ""

    def wrap(self, command: str) -> str:
        """``command``, rewritten to run inside this environment.

        Identity when the plan has no way in. A plan that stands a container up
        and then keeps running the commands on the host would have isolated
        nothing while claiming to — so this is the seam that must be used, not
        an optional convenience.
        """
        if not self.exec_template:
            return command
        return self.exec_template.replace(_COMMAND_SLOT, shlex.quote(command))


def _from_metadata(
    raw: Any, project: str, workspace_path: str, files: set[str]
) -> StackPlan | None:
    if not isinstance(raw, Mapping):
        raise StackPlanError(
            f"{_METADATA_KEY} must be a mapping with 'up' and 'down' (got {type(raw).__name__})"
        )
    image = str(raw.get("image") or "").strip()
    if image and not (raw.get("up") or raw.get("down")):
        # Only the image is overridden: the derivation is right for this repo,
        # its default interpreter is not. So it re-derives with that image —
        # dropping a declared compose stack because someone pinned an
        # interpreter would silently verify a stackless version of the product.
        return _derived_plan(
            files=files, project=project, workspace_path=workspace_path, image=image
        )
    up = str(raw.get("up") or "").strip()
    down = str(raw.get("down") or "").strip()
    if not up or not down:
        # A stand-up with no matching tear-down leaks a stack for good; a
        # tear-down with no stand-up never runs. Half a declaration is a
        # misconfiguration, not a partial feature.
        raise StackPlanError(f"{_METADATA_KEY} needs both 'up' and 'down'")
    # ``exec`` carries ``{command}`` through untouched — it binds per command.
    exec_template = str(raw.get("exec") or "").strip().replace("{project}", project)
    return StackPlan(
        up=up.format(project=project),
        down=down.format(project=project),
        source="metadata",
        image=image or None,
        exec_template=exec_template,
        workdir=str(raw.get("workdir") or "").strip(),
    )


def _source_container_up(*, name: str, image: str, workspace_path: str, network: str = "") -> str:
    """Stand one idle container up holding a clean copy of the source.

    The copy is a tar stream rather than a bind mount so the founder's tree is
    never written to (verification must not mutate the directory the agent works
    in) and so it does not depend on which host paths the docker VM shares.

    Nothing is installed here. Provisioning is the CHECK commands' job — a
    repo-agnostic guess at an install step is exactly the invention the derived
    gate refuses to make.

    ``network`` puts it on an existing bridge. That is the ONLY difference
    between the CLI product's environment and a compose product's prober, and it
    is what turns "a stack is up somewhere" into "a check can talk to it".
    """
    quoted = shlex.quote(name)
    excludes = " ".join(shlex.quote(f"--exclude={pattern}") for pattern in _UNCOPYABLE)
    joins = f"--network {shlex.quote(network)} " if network else ""
    # ⚠️ The copy is a PIPELINE, and a pipeline's exit status is its LAST stage:
    # a failing ``tar -cf`` (missing path, unreadable tree) still leaves the
    # receiving ``tar -xf`` exiting 0. That would boot a container with an EMPTY
    # /work whose checks then find no manifest — an infrastructure fault wearing
    # the costume of "this repo has no gate", which is the exact disguise that
    # cost a day in the 2026-08-09 E2E. ``pipefail`` is not POSIX (dash lacks
    # it), so absence proves itself instead: the source must be THERE afterwards.
    copy_in = (
        f"tar -cf - -C {shlex.quote(workspace_path)} {excludes} . "
        f"| docker exec -i {quoted} tar -xf - -C {_CONTAINER_WORKDIR}"
    )
    copied = f"docker exec {quoted} sh -lc '[ -n \"$(ls -A {_CONTAINER_WORKDIR})\" ]'"
    # ``sleep infinity`` because the container is a place to exec into, not a
    # service; it holds still until teardown.
    return (
        f"docker run -d --name {quoted} {joins}-w {_CONTAINER_WORKDIR} "
        f"{shlex.quote(image)} sleep infinity && {copy_in} && {copied}"
    )


def _exec_into(name: str) -> str:
    return f"docker exec -w {_CONTAINER_WORKDIR} {shlex.quote(name)} sh -lc {_COMMAND_SLOT}"


def _container_plan(*, image: str, project: str, workspace_path: str) -> StackPlan:
    """A CLI product's whole environment: one container holding its source."""
    return StackPlan(
        up=_source_container_up(name=project, image=image, workspace_path=workspace_path),
        # ``rm -f`` is idempotent (exit 0 when there is nothing there), which is
        # what lets the next slot holder clear a dead holder's leftovers.
        down=f"docker rm -f {shlex.quote(project)}",
        source="container",
        image=image,
        exec_template=_exec_into(project),
        workdir=_CONTAINER_WORKDIR,
    )


def _compose_plan(*, project: str, workspace_path: str, image: str | None) -> StackPlan:
    """A compose product: the stack, plus a PROBER that can actually reach it.

    The overlay publishes no host ports — deliberately, because that is what
    stops the disposable stack fighting production for 5442/6387/8700/3700. The
    consequence is that a command on the founder's machine cannot reach the
    stack it just stood up; ``localhost:8700`` is production's, or nobody's.

    So the checks run in a container joined to the stack's own network, where
    compose's embedded DNS answers for the service names the product uses among
    itself (``backend:8000``, ``postgres:5432``). It holds the run's source, so
    a probe suite committed to the repo can run against a live instance of the
    change that produced it.

    ⚠️ **Teardown order is load-bearing, and its failure is silent.** Measured
    2026-08-11: ``docker compose down -v`` cannot remove a network that still
    has an endpoint attached — it prints "Resource is still in use" and exits
    **0**. Leaving the prober attached therefore leaks one network per run while
    reporting success. The prober goes first, and its removal is idempotent so
    the slot's defensive teardown still clears a dead holder's leftovers.

    ``image is None`` — a compose repo declaring no toolchain at all — keeps the
    stack and leaves the checks where they were. Guessing an image would be the
    fabrication the derived gate refuses to make.
    """
    compose = f"docker compose -p {project} -f {_COMPOSE_BASE} -f {_COMPOSE_VERIFY}"
    # ``-v`` because the volumes are the disk cost, and a full disk on the
    # founder's machine is unrecoverable.
    teardown = f"{compose} down -v"
    if image is None:
        return StackPlan(up=f"{compose} up -d --wait", down=teardown, source="compose")

    prober = f"{project}{_PROBE_SUFFIX}"
    return StackPlan(
        up=(
            f"{compose} up -d --wait && "
            + _source_container_up(
                name=prober,
                image=image,
                workspace_path=workspace_path,
                network=f"{project}{_COMPOSE_NETWORK_SUFFIX}",
            )
        ),
        # ``;`` not ``&&``: both halves are idempotent and each must run even if
        # the other found nothing to do.
        down=f"docker rm -f {shlex.quote(prober)}; {teardown}",
        source="compose",
        image=image,
        exec_template=_exec_into(prober),
        workdir=_CONTAINER_WORKDIR,
    )


def _derive_image(files: set[str]) -> str | None:
    for manifests, image in _TOOLCHAIN_IMAGES:
        if any(m in files for m in manifests):
            return image
    return None


def _derived_plan(
    *, files: set[str], project: str, workspace_path: str, image: str | None = None
) -> StackPlan | None:
    """What this repo declares, boiled down to one plan.

    ``image`` overrides the DERIVED image only — never which kind of environment
    the repo declares. The two questions are independent: "is this a stack or a
    lone toolchain" is answered by the files, "which interpreter" can be
    answered by the product.
    """
    if _COMPOSE_BASE in files:
        if _COMPOSE_VERIFY not in files:
            raise StackPlanError(
                f"{_COMPOSE_BASE} is present but the isolation overlay {_COMPOSE_VERIFY} is not — "
                "standing the base stack up on this host would collide with production's "
                "container_name / ports / image tag. Refusing to boot unisolated."
            )
        return _compose_plan(
            project=project,
            workspace_path=workspace_path,
            image=image or _derive_image(files),
        )

    chosen = image or _derive_image(files)
    if chosen is None:
        # Nothing declared means nothing to reproduce. A container built on a
        # guess would be a fabricated environment, which is worse than none.
        return None
    return _container_plan(image=chosen, project=project, workspace_path=workspace_path)


def derive_stack_plan(
    *,
    repo_files: Sequence[str],
    project: str,
    workspace_path: str,
    metadata: Mapping[str, Any] | None = None,
) -> StackPlan | None:
    """The stand-up / tear-down plan for one disposable instance, or ``None``.

    ``None`` means the checks run where the box already runs them — the honest
    answer for a repo that declares no toolchain to reproduce, and the declared
    answer for a product whose checks genuinely need host resources
    (``verify_stack: null``).

    ``project`` is the compose project / container name, which the caller takes
    from the held verification SLOT: naming the instance after the slot is what
    makes the next acquirer clean up a dead holder's leftovers (see
    :mod:`backend.workflow.infrastructure.verify_slots`).

    ``workspace_path`` is where the source sits FROM THE BOX'S POINT OF VIEW —
    the copy runs through the same box as the stand-up.
    """
    files = set(repo_files)
    if metadata is not None and _METADATA_KEY in metadata:
        raw = metadata[_METADATA_KEY]
        if raw is None:
            return None
        return _from_metadata(raw, project, workspace_path, files)
    return _derived_plan(files=files, project=project, workspace_path=workspace_path)


__all__ = ["StackPlan", "StackPlanError", "derive_stack_plan"]
