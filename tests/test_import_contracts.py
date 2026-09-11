"""Lift N defensive pattern #2 + #9 + #10 — import-linter smoke gate.

These tests are the **test-mode-independent** invariant guard for the
import-linter contracts defined in :file:`pyproject.toml` (Lift N
defensive pattern #10 / v8 §22). They run inside the standard pytest
suite so a contributor cannot break the architectural baseline without
the CI gate flagging it locally first.

Why a test, when CI already runs ``uv run lint-imports``? — Defensive
pattern #10: critical invariants must be checkable without any "skip"
flag. If a contributor disables the CI step they still hit the test
locally; if they disable the test the CI step still fires. Two
independent enforcement seams for one architectural rule.
"""

from __future__ import annotations

import subprocess


def test_import_linter_contracts_pass() -> None:
    """``uv run lint-imports`` must return exit code 0 across the full repo.

    The contracts checked (see ``[tool.importlinter]`` in
    :file:`pyproject.toml`):

    * ``bsvibe_sdk`` has zero ``backend`` / ``plugin`` imports — the SDK
      is a leaf so external plugin authors never pull in the engine.
    * Common leaves (``backend.shared`` / ``backend.data`` /
      ``backend.auth`` / ``backend.embedding`` / ``backend.notifications``
      / ``backend.workers``) do not import bounded contexts — the
      direction-of-dependency invariant (v8 §22 #2 / D45).
    * Connector plugins (``plugin.discord`` …) depend only on
      ``bsvibe_sdk`` plus the small published-seam allow-list.
    * The MCP context depends only on Identity + Workflow + Knowledge +
      the common leaves.
    * Identity does not depend on Executors — the authorization server
      does not import its own clients.
    """
    # Call import-linter via the same console entry-point CI uses; using
    # the Python API directly would skip the config-discovery code path
    # whose health is the point of this smoke test.
    import shutil

    cli = shutil.which("lint-imports")
    assert cli is not None, "lint-imports CLI missing from venv — install dev deps"
    result = subprocess.run(  # noqa: S603
        [cli],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "import-linter contracts BROKEN — Lift N defensive baseline regressed.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


#: The contract whose ABSENCE this file exists to notice. ``lint-imports``
#: exits 0 on a config with zero contracts, so "the gate is green" says
#: nothing about whether the gate is still installed — deleting the rule from
#: :file:`pyproject.toml` would look identical to obeying it.
_IDENTITY_EXECUTORS_CONTRACT = "Identity does not depend on Executors"


def test_identity_does_not_import_executors_contract_is_installed() -> None:
    """The Identity→Executors rule must still BE a contract, not just pass.

    It was added because the previous check could not fail: no contract named
    ``backend.identity`` as a source module, so importing the worker CLI's
    login module into the authorization server was structurally guaranteed a
    green ``lint-imports`` run. A rule that can be silently deleted has the
    same defect one layer up, so this pins the name into the report.
    """
    import shutil

    cli = shutil.which("lint-imports")
    assert cli is not None, "lint-imports CLI missing from venv — install dev deps"
    result = subprocess.run(  # noqa: S603
        [cli],
        capture_output=True,
        text=True,
        check=False,
    )
    assert _IDENTITY_EXECUTORS_CONTRACT in result.stdout, (
        "the Identity→Executors import contract is no longer declared in "
        "pyproject.toml — the server may quietly start importing its clients "
        f"again.\nstdout:\n{result.stdout}"
    )
