"""``compose.prod.yaml`` must not answer the sandbox question on the founder's behalf.

`backend/config.py` refuses to start in prod when ``BSVIBE_SANDBOX_ENABLED`` was
never provided — silence is not a decision, and an unset flag degrades agent
execution to the worker container's host with no guard firing.

That check is defeated one layer down. ``${BSVIBE_SANDBOX_ENABLED:-false}``
means compose SUPPLIES ``"false"`` when the variable is absent, so Settings
always sees it as explicitly provided and the validator can never fire. Measured
2026-08-26 with `docker compose config` and no variable in the environment:

    environment:
      BSVIBE_SANDBOX_ENABLED: "false"

A `.env.prod` that fails to load therefore still produces a silent host run —
now wearing the costume of a deliberate choice. The typed layer was fixed and
the deployment layer, which is the one that actually runs, was not. Sibling
failure to the one `test_verify_env_passthrough` was written for: "a config
that is configured everywhere except where it runs".

``:?`` is the form that refuses: compose itself errors out when the variable is
missing, and an explicit ``false`` still passes through untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO / "deploy" / "compose.prod.yaml"

#: The founder's own decision must reach the container, never a value compose
#: invented. Any setting whose accidental default is DANGEROUS (rather than
#: merely conservative) belongs here.
_MUST_BE_DECIDED = ("BSVIBE_SANDBOX_ENABLED",)


def _assignments(name: str) -> list[str]:
    text = _COMPOSE.read_text(encoding="utf-8")
    return re.findall(rf"^\s*{re.escape(name)}:\s*(.+)$", text, flags=re.MULTILINE)


@pytest.mark.parametrize("name", _MUST_BE_DECIDED)
def test_compose_does_not_supply_a_default(name: str) -> None:
    """``:-`` invents an answer; ``:?`` demands one."""
    values = _assignments(name)
    assert values, f"{name} is not passed through to any service in compose.prod.yaml"
    for raw in values:
        assert ":-" not in raw, (
            f"{name} uses a compose DEFAULT ({raw.strip()}) — an absent .env.prod then "
            "looks identical to a deliberate choice, and the Settings guard cannot fire."
        )


@pytest.mark.parametrize("name", _MUST_BE_DECIDED)
def test_compose_requires_the_variable(name: str) -> None:
    """Every service that reads it must refuse to start without it — one service
    left on ``:-`` is the whole hole, since that is the container that runs the
    agent's commands."""
    values = _assignments(name)
    for raw in values:
        assert ":?" in raw, f"{name} must use the required form: {raw.strip()}"


def test_both_services_still_receive_it() -> None:
    """POSITIVE CONTROL — the guard must not be satisfiable by DELETING the
    passthrough. backend and worker both read it."""
    assert len(_assignments("BSVIBE_SANDBOX_ENABLED")) >= 2
