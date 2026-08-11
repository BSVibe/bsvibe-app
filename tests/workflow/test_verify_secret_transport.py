"""A declared secret reaches the check, and appears nowhere else.

The exec channel carries ONE shell command string to the founder's machine, and
that string is persisted verbatim on ``executor_tasks.prompt`` (Text, unbounded)
and published on a Redis stream that is never trimmed. So a secret interpolated
into the command — ``docker run -e PASSWORD=hunter2`` — is a secret written to
the database and kept in Redis for good.

It travels beside the command instead, on the dispatch payload only. That is not
a new idea here: the per-run MCP token already rides exactly this way, with the
reason stated at the seam — *"Not persisted on the row: the token is ephemeral
and belongs to this dispatch only."*

The command then names the variables without valuing them (``docker run -e
NAME``, which docker fills from the invoking process's environment), so what is
persisted is a list of names — useful for reading the evidence trail, useless to
anyone who finds it.

Every test below asserts the same thing from a different angle: the plaintext is
in the container and in no artifact.
"""

from __future__ import annotations

import uuid

import pytest

from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.domain.verify_secrets import METADATA_KEY, seal_secrets
from backend.workflow.domain.verify_stack import derive_stack_plan

pytestmark = pytest.mark.asyncio

_SECRET = "hunter2-do-not-leak"  # noqa: S105 — the string this file hunts for
_KEY = b"0123456789abcdef0123456789abcdef"
_CIPHER = CredentialCipher(_KEY)


@pytest.fixture
def kms_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the seam THIS file's key, without touching settings.

    The obvious version — set ``BSVIBE_GATEWAY_KMS_KEY_B64`` and
    ``get_settings.cache_clear()`` — pollutes the whole session twice over: the
    cache is global, so clearing it makes every later reader rebuild settings
    from whatever the environment happens to be at that moment, and a fixture
    requesting ``monkeypatch`` is finalised BEFORE it, so the clear re-caches
    this test's key. Both were observed: unrelated glue and alembic tests
    failing in the full run and passing in isolation, with a different set each
    time.

    Patching the key FUNCTION instead touches nothing global.
    """
    monkeypatch.setattr(
        "backend.router.accounts.crypto._key_from_settings", lambda: _KEY, raising=True
    )


def _metadata() -> dict[str, object]:
    return seal_secrets({METADATA_KEY: {"BSVIBE_TEST_PASSWORD": _SECRET}}, encrypt=_CIPHER.encrypt)


class TestTheCommandNamesButDoesNotValue:
    def test_the_stack_command_carries_the_name_only(self) -> None:
        plan = derive_stack_plan(
            repo_files=["pyproject.toml"],
            project="verify-slot-0",
            workspace_path="/ws",
            metadata=_metadata(),
        )

        assert plan is not None
        assert "-e BSVIBE_TEST_PASSWORD" in plan.up
        assert _SECRET not in plan.up, (
            "the value reached the command string, which is persisted on "
            f"executor_tasks.prompt: {plan.up!r}"
        )

    def test_a_product_declaring_nothing_gets_no_env_flags(self) -> None:
        plan = derive_stack_plan(repo_files=["pyproject.toml"], project="p", workspace_path="/ws")

        assert plan is not None
        assert " -e " not in plan.up

    def test_the_names_are_ordered_so_the_command_is_comparable(self) -> None:
        metadata = seal_secrets(
            {METADATA_KEY: {"B_KEY": "2", "A_KEY": "1"}}, encrypt=_CIPHER.encrypt
        )
        plan = derive_stack_plan(
            repo_files=["pyproject.toml"], project="p", workspace_path="/ws", metadata=metadata
        )

        assert plan is not None
        assert plan.up.index("-e A_KEY") < plan.up.index("-e B_KEY")

    def test_a_compose_products_prober_gets_them_too(self) -> None:
        """The browser probe's whole reason for existing. A compose product's
        checks run in the prober (#737), so that is where its login credential
        has to arrive."""
        metadata = _metadata()
        metadata["verify_stack"] = {"image": "mcr.microsoft.com/playwright:v1.50.0"}
        plan = derive_stack_plan(
            repo_files=["deploy/compose.yaml", "deploy/compose.verify.yaml"],
            project="verify-slot-0",
            workspace_path="/ws",
            metadata=metadata,
        )

        assert plan is not None
        assert plan.source == "compose"
        assert "-e BSVIBE_TEST_PASSWORD" in plan.up
        assert _SECRET not in plan.up


class TestTheValueTravelsBesideTheCommand:
    async def test_the_worker_gets_it_and_the_row_does_not(self) -> None:
        """The load-bearing assertion of this whole file."""
        from backend.executors import dispatch
        from tests._support import shared_file_sessionmaker

        published: list[dict[str, object]] = []

        class _Redis:
            async def xadd(self, name: str, fields: dict[str, object], **kw: object) -> str:
                published.append(dict(fields))
                return "1-0"

        async with shared_file_sessionmaker() as sf:
            async with sf() as session:
                task = await dispatch.create_task(
                    session,
                    workspace_id=uuid.uuid4(),
                    executor_type="claude_code",
                    prompt="docker run -e BSVIBE_TEST_PASSWORD img sh -lc 'echo hi'",
                    workspace_dir="/ws",
                    execution_target="client_attach",
                )
                task_id = task.id
                await dispatch.dispatch_task(
                    _Redis(),
                    session=session,
                    task=task,
                    worker_id=uuid.uuid4(),
                    action="exec",
                    env={"BSVIBE_TEST_PASSWORD": _SECRET},
                )
                await session.commit()

            async with sf() as session:
                from backend.executors.db import ExecutorTaskRow

                row = await session.get(ExecutorTaskRow, task_id)
                assert row is not None
                persisted = " ".join(
                    str(v) for v in (row.prompt, row.system, row.workspace_dir, row.output)
                )

        assert _SECRET not in persisted, (
            "the secret was written to executor_tasks — that row outlives the run "
            "and lands in every dump and backup"
        )
        assert any(_SECRET in str(fields) for fields in published), (
            "the secret never reached the worker at all"
        )


class TestNothingLogsIt:
    async def test_the_dispatch_log_carries_names_not_values(self) -> None:
        """A secret in a log line is a secret in the log aggregator, on disk, and
        in whatever ships logs onward. Names are enough to debug with."""
        import structlog

        from backend.executors import dispatch
        from tests._support import shared_file_sessionmaker

        events: list[dict[str, object]] = []

        def _capture(_logger: object, _name: str, event: dict[str, object]) -> dict[str, object]:
            events.append(dict(event))
            return event

        original = structlog.get_config()["processors"]
        structlog.configure(processors=[_capture, *original])
        try:
            async with shared_file_sessionmaker() as sf, sf() as session:
                task = await dispatch.create_task(
                    session,
                    workspace_id=uuid.uuid4(),
                    executor_type="claude_code",
                    prompt="echo hi",
                    workspace_dir="/ws",
                    execution_target="client_attach",
                )

                class _Redis:
                    async def xadd(self, name: str, fields: dict[str, object], **kw: object) -> str:
                        return "1-0"

                await dispatch.dispatch_task(
                    _Redis(),
                    session=session,
                    task=task,
                    worker_id=uuid.uuid4(),
                    action="exec",
                    env={"BSVIBE_TEST_PASSWORD": _SECRET},
                )
        finally:
            structlog.configure(processors=original)

        assert events, "no log events captured — the assertion below would be vacuous"
        assert not any(_SECRET in str(event) for event in events)


class TestTheValueReachesTheBootAndNothingElse:
    """NC gap found the hard way: every assertion above held while the values
    never reached the boot command at all. Shape without delivery."""

    async def test_the_boot_command_gets_them_and_later_commands_do_not(self) -> None:
        """Only ``docker run`` needs them in the invoking process's environment.
        Every later command runs INSIDE that container, which already carries
        them — re-sending would widen the exposure to buy nothing."""
        from backend.workflow.application.verification_stack import open_verification_stack

        calls: list[tuple[str, dict[str, object]]] = []

        class _Box:
            workspace_mount = "/ws"

            async def exec(self, command: str, **kwargs: object) -> object:
                from backend.workflow.infrastructure.sandbox import SandboxResult

                calls.append((command, dict(kwargs)))
                return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)

        async with open_verification_stack(
            box=_Box(),
            slot_project="verify-slot-0",
            repo_files=["pyproject.toml"],
            metadata=_metadata(),
            docker_context="colima",
            boot_timeout_s=60.0,
            secrets={"BSVIBE_TEST_PASSWORD": _SECRET},
        ) as outcome:
            assert type(outcome).__name__ == "StackReady"

        booted = [kw for cmd, kw in calls if "docker run" in cmd]
        assert booted, "the stack never booted"
        assert booted[0].get("env") == {"BSVIBE_TEST_PASSWORD": _SECRET}, (
            "the declared secret never reached the boot — `docker run -e NAME` "
            "then resolves to nothing and every check runs without it"
        )
        teardowns = [kw for cmd, kw in calls if "docker rm" in cmd]
        assert all("env" not in kw for kw in teardowns)

    async def test_a_product_without_secrets_boots_exactly_as_before(self) -> None:
        """No ``env`` kwarg at all — so a backend that cannot carry one is
        untouched by this change unless a product actually asks for it."""
        from backend.workflow.application.verification_stack import open_verification_stack

        seen: list[dict[str, object]] = []

        class _Box:
            workspace_mount = "/ws"

            async def exec(self, command: str, **kwargs: object) -> object:
                from backend.workflow.infrastructure.sandbox import SandboxResult

                seen.append(dict(kwargs))
                return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)

        async with open_verification_stack(
            box=_Box(),
            slot_project="p",
            repo_files=["pyproject.toml"],
            metadata=None,
            docker_context="colima",
            boot_timeout_s=60.0,
        ):
            pass

        assert all("env" not in kw for kw in seen)

    async def test_the_environment_seam_unseals_what_the_product_declared(
        self, kms_key: None
    ) -> None:
        """The other half of the wiring: sealed metadata in, plaintext out, and
        actually handed over. Without this the two halves can both be right and
        never meet."""
        import backend.workflow.application.verify_environment as ve

        assert ve._unseal(_metadata()) == {"BSVIBE_TEST_PASSWORD": _SECRET}
        assert ve._unseal({}) == {}
