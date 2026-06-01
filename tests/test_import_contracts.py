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
