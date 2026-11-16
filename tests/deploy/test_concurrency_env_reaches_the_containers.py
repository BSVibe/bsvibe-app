"""The concurrency settings reach the containers that read them, defaults unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose.prod.yaml"
_DEFAULTS = {
    "BSVIBE_AGENT_DRIVE_CONCURRENCY": "0",
    "BSVIBE_SANDBOX_MAX_CONCURRENT": "2",
    "BSVIBE_VERIFY_STACK_SLOTS_TOTAL": "1",
}


class _ComposeLoader(yaml.SafeLoader):
    """Compose's own tags (``!reset``, ``!override``) read as their plain value."""


_ComposeLoader.add_multi_constructor("!", lambda _loader, _suffix, _node: None)


def _services() -> dict:
    return yaml.load(_COMPOSE.read_text(encoding="utf-8"), Loader=_ComposeLoader)["services"]  # noqa: S506


@pytest.mark.parametrize("service", ["backend", "worker"])
@pytest.mark.parametrize("name", list(_DEFAULTS))
def test_defaults_unchanged(service: str, name: str) -> None:
    env = _services()[service]["environment"]

    assert env[name] == f"${{{name}:-{_DEFAULTS[name]}}}"
