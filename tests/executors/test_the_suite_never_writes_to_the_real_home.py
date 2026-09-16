"""The executor suite must never create anything in the founder's real home.

#973 gave ``handle_task`` a DEFAULT sandbox cwd under ``$BSVIBE_HOME`` that it
``os.makedirs``. Before that, the same call made a throwaway temp dir, so a test
calling it without an explicit ``sandbox_cwd`` was harmless. Now it is not —
measured after #973: a plain ``pytest tests/`` run left an empty
``~/.bsvibe/sandbox-cwd`` in the real home, because
``tests/executors/test_dispatch_deadline_reaches_the_worker.py`` sat one level
OUTSIDE the HOME-redirect fixture that lived in ``tests/executors/worker/``.

The fixture moved up to ``tests/executors/``. This pins that it actually reaches
here, so the next default-to-home path added anywhere in this package is caught
by a red test rather than by noticing a stray directory on the host.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.executors.worker.config import default_sandbox_cwd
from backend.executors.worker.credentials import default_worker_token_path


def test_bsvibe_home_is_redirected_away_from_the_real_home() -> None:
    """The autouse fixture in this package's conftest must be in force HERE."""
    base = os.environ.get("BSVIBE_HOME")
    assert base, "BSVIBE_HOME is unset — the HOME-redirect fixture does not reach this file"
    assert Path(base).resolve() != (Path.home() / ".bsvibe").resolve()


def test_every_home_defaulting_path_lands_under_the_redirect() -> None:
    """Pin the RESULT SET — each path that defaults to the home dir, not a
    remembered list of names. A new one added without a redirect fails here."""
    redirect = Path(os.environ["BSVIBE_HOME"]).resolve()
    real_home = Path.home().resolve()

    for path in (default_sandbox_cwd(), default_worker_token_path()):
        resolved = Path(path).resolve()
        assert redirect in resolved.parents or resolved == redirect, resolved
        assert real_home not in resolved.parents or redirect.is_relative_to(real_home), resolved
