"""A disposable verification stack must not be able to kill production.

The full-surface verification design stands the whole stack up on the SAME
machine that runs prod, per run, then tears it down. ``deploy/compose.yaml`` as
written cannot do that safely:

* ``container_name: bsvibe-sandbox-dind`` is a FIXED name. ``-p <project>`` does
  not scope it, so a second stack collides with the container prod is running —
  and a compose name collision has already taken this stack down once
  (memory: "container_name 충돌로 스택다운").
* published host ports (5442/6387/8700/3700) collide.
* ``image: bsvibe-sandbox-dind:latest`` is a SHARED tag — building it from a
  feature branch repoints the tag prod recreates from.

``deploy/compose.verify.yaml`` is the overlay that neutralises all three. This
module is the DRIFT GUARD: add a published port / fixed name / shared image tag
to the base later and the test fails until the overlay covers it too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
_BASE = _DEPLOY / "compose.yaml"
_VERIFY = _DEPLOY / "compose.verify.yaml"


class _ComposeLoader(yaml.SafeLoader):
    """Compose's ``!reset`` extension is not plain YAML — teach the loader."""


def _reset(loader: yaml.Loader, node: yaml.Node) -> object:
    return _RESET


class _Reset:
    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return "!reset"


_RESET = _Reset()
_ComposeLoader.add_constructor("!reset", _reset)


def _load(path: Path) -> dict[str, Any]:
    return yaml.load(path.read_text(), Loader=_ComposeLoader)  # noqa: S506 — custom safe subclass


def _services(doc: dict[str, Any]) -> dict[str, Any]:
    return doc.get("services") or {}


def _base_services_publishing_ports() -> list[str]:
    return [name for name, svc in _services(_load(_BASE)).items() if svc.get("ports")]


def _base_services_with_fixed_name() -> list[str]:
    return [name for name, svc in _services(_load(_BASE)).items() if svc.get("container_name")]


def _base_services_with_shared_image_tag() -> list[str]:
    # A service that BUILDS and also pins an explicit image tag shares that tag
    # across every project on the host. (An image with no build — postgres,
    # redis — is a pulled upstream tag; harmless.)
    return [
        name
        for name, svc in _services(_load(_BASE)).items()
        if svc.get("build") and svc.get("image")
    ]


def test_the_verify_overlay_exists() -> None:
    assert _VERIFY.is_file(), "the disposable stack has no isolation overlay"


@pytest.mark.parametrize("service", _base_services_publishing_ports())
def test_every_published_port_is_reset_in_verify(service: str) -> None:
    """No host ports at all: probes reach the stack over the compose network, so
    there is nothing to collide with prod's bindings."""
    svc = _services(_load(_VERIFY)).get(service) or {}
    ports = svc.get("ports", "MISSING")
    assert ports is _RESET or ports == [], (
        f"{service} publishes host ports in the base compose but the verify "
        f"overlay does not reset them (got {ports!r})"
    )


@pytest.mark.parametrize("service", _base_services_with_fixed_name())
def test_every_fixed_container_name_is_neutralised(service: str) -> None:
    """``-p`` does not scope ``container_name`` — an un-neutralised fixed name
    collides with the container prod is currently running."""
    svc = _services(_load(_VERIFY)).get(service) or {}
    assert "container_name" in svc, (
        f"{service} pins container_name in the base compose; the verify overlay "
        "must reset it or the second stack collides with prod"
    )
    assert svc["container_name"] is _RESET


@pytest.mark.parametrize("service", _base_services_with_shared_image_tag())
def test_shared_image_tags_are_not_rebuilt_over(service: str) -> None:
    """Building a shared tag from a feature branch repoints what prod recreates
    from — contamination through the image registry rather than the network."""
    svc = _services(_load(_VERIFY)).get(service) or {}
    base_tag = (_services(_load(_BASE))[service]).get("image")
    assert svc.get("image") and svc["image"] != base_tag, (
        f"{service} pins the shared tag {base_tag!r}; the verify overlay must "
        "build under a different tag"
    )


def test_verify_overlay_publishes_no_ports_at_all() -> None:
    """Belt and braces: the overlay itself must never ADD a binding."""
    for name, svc in _services(_load(_VERIFY)).items():
        ports = svc.get("ports")
        assert ports is _RESET or not ports, f"{name} publishes ports in the verify overlay"
