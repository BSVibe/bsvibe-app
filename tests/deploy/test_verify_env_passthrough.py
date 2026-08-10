"""The verification settings must actually REACH the containers.

``compose.prod.yaml`` passes an explicit ALLOWLIST of environment variables —
adding a value to ``.env.prod`` does nothing on its own. So a setting the
verification path reads keeps its default in production while the code, the
tests, and the ``.env.prod.example`` all say otherwise: a config that is
"configured" everywhere except where it runs.

That is not hypothetical. ``BSVIBE_VERIFY_DOCKER_CONTEXT`` was set in
``.env.prod`` on 2026-08-10 and was absent from both containers, because the
passthrough was missing — the docker pin silently stayed off.

Why THIS setting is worth a static guard: unpinned, the disposable environment
is stood up on whichever daemon the host's global context points at and torn
down against whichever it points at LATER. The container leaks, and "reclaiming
the slot reclaims the stack" — the property that makes the whole slot design
work without a reaper — quietly stops holding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO / "deploy"

#: Settings the verification environment reads at run time. A default that is
#: WRONG in production (rather than merely conservative) belongs here.
_MUST_REACH_THE_CONTAINERS = ("BSVIBE_VERIFY_DOCKER_CONTEXT",)


@pytest.fixture(scope="module")
def compose_prod() -> str:
    return (_DEPLOY / "compose.prod.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", _MUST_REACH_THE_CONTAINERS)
def test_the_verification_settings_are_passed_through(compose_prod: str, name: str) -> None:
    """Once for each service that runs a verification: the backend and the
    worker. Both appear in ``compose.prod.yaml``'s environment allowlists."""
    assert compose_prod.count(f"{name}: ${{{name}") == 2, (
        f"{name} must be passed through to BOTH backend and worker — an "
        "allowlist that omits it leaves the setting at its default in prod, "
        "however thoroughly .env.prod says otherwise"
    )


@pytest.mark.parametrize("name", _MUST_REACH_THE_CONTAINERS)
def test_the_example_env_documents_them(name: str) -> None:
    """A passthrough nobody knows to set is a passthrough that stays empty."""
    example = (_DEPLOY / ".env.prod.example").read_text(encoding="utf-8")
    assert name in example
