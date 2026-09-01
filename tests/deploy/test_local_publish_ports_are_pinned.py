"""``compose.yaml``'s host publish ports must stay put when unconfigured.

The four host ports it publishes (backend 8700, postgres 5442, redis 6387,
pwa 3700) can collide with another compose project already running on the
same machine (e.g. a prod stack — see ``deploy/README.md`` §9). Making them
overridable via ``${VAR:-default}`` must not, on its own, change what a plain
``docker compose -f deploy/compose.yaml up -d`` (no env vars set) binds to.

Pin the VALUES, not merely their count: a parameterization that renders to
the wrong default passes a count-only check just as easily as the right one.

⚠️ This reads the compose TEXT rather than ``docker compose config`` output on
purpose. The verification sandbox has no docker binary, so a gate shelling out
to the daemon fails there for reasons unrelated to the proposition — it would
be red whether the ports were right or wrong, which is no signal at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO / "deploy" / "compose.yaml"

#: container_port -> expected default host port, when the override env var is unset.
_EXPECTED_DEFAULT_PUBLISH_PORTS = {
    "5432": "5442",  # postgres
    "6379": "6387",  # redis
    "8000": "8700",  # backend
    "3700": "3700",  # pwa
}


@pytest.fixture(scope="module")
def compose_text() -> str:
    return _COMPOSE.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "container_port,expected_default", sorted(_EXPECTED_DEFAULT_PUBLISH_PORTS.items())
)
def test_default_publish_port_is_unchanged(
    compose_text: str, container_port: str, expected_default: str
) -> None:
    """Every publish mapping is a ``${VAR:-default}`` form whose default is
    exactly the pre-existing value (a hardcoded port fails this to begin with)."""
    match = re.search(rf'"\$\{{\w+:-(\d+)\}}:{container_port}"', compose_text)
    assert match, (
        f"expected a parameterized host-port publish mapping ending in "
        f':{container_port}" (the compose ${{VAR:-default}} form) in deploy/compose.yaml'
    )
    assert match.group(1) == expected_default, (
        f"default publish port for container port {container_port} changed to "
        f"{match.group(1)!r} — it must stay {expected_default!r} when the override "
        "env var is unset, or it silently moves a port other stacks rely on"
    )
