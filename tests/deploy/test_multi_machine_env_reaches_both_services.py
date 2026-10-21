"""#929 — the multi-machine settings reach the containers that read them.

``deploy/compose.prod.yaml`` passes each setting by name; a variable that is only in ``.env.prod``
never reaches a container (HANDOFF §Ⅱ: the run token cap sat unwired that way). Found
2026-10-21: ``BSVIBE_MACHINE_ID`` (#1189) was documented in ``.env.prod.example`` but never
passed, so verify slots, ``home_machine``, run checkpoints and tool-call forwarding would all
have stayed off on every box however it was set.

Both the backend (MCP work tools, tool-call forwarding, the vault for REST) and the worker (claim,
checkpoint, verify, settle) read both settings.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose.prod.yaml"

_SETTINGS = ("BSVIBE_MACHINE_ID", "BSVIBE_KNOWLEDGE_STORE_BACKEND")


class _ComposeLoader(yaml.SafeLoader):
    """Compose's own tags (``!reset``, ``!override``) read as their plain value."""


_ComposeLoader.add_multi_constructor("!", lambda _loader, _suffix, _node: None)


def _services() -> dict:
    return yaml.load(_COMPOSE.read_text(encoding="utf-8"), Loader=_ComposeLoader)["services"]  # noqa: S506


@pytest.mark.parametrize("service", ["backend", "worker"])
@pytest.mark.parametrize("name", _SETTINGS)
def test_the_setting_reaches_the_service(service: str, name: str) -> None:
    env = _services()[service]["environment"]

    assert name in env, f"{name} never reaches the {service} container"


def test_a_single_box_stays_a_single_box_by_default() -> None:
    env = _services()["backend"]["environment"]

    assert env["BSVIBE_MACHINE_ID"] == "${BSVIBE_MACHINE_ID:-}"
    assert env["BSVIBE_KNOWLEDGE_STORE_BACKEND"] == "${BSVIBE_KNOWLEDGE_STORE_BACKEND:-filesystem}"
