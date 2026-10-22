"""Every KMS variable ``crypto`` reads must reach the backend and worker containers.

``deploy/compose.prod.yaml`` passes each setting by name. ``BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64``
was only in ``.env.prod``, so a runbook §3 rotation left the containers with the new key alone and
every row sealed with the old one became ``UnknownCredentialKey``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO / "deploy" / "compose.prod.yaml"
_CRYPTO = _REPO / "backend" / "router" / "accounts" / "crypto.py"


class _ComposeLoader(yaml.SafeLoader):
    """Compose's own tags (``!reset``, ``!override``) read as their plain value."""


_ComposeLoader.add_multi_constructor("!", lambda _loader, _suffix, _node: None)


def _env(service: str) -> dict:
    doc = yaml.load(_COMPOSE.read_text(encoding="utf-8"), Loader=_ComposeLoader)  # noqa: S506
    return doc["services"][service]["environment"]


def _kms_vars_crypto_reads() -> set[str]:
    return set(re.findall(r"BSVIBE_GATEWAY_KMS_\w+_B64", _CRYPTO.read_text(encoding="utf-8")))


def test_crypto_reads_both_the_current_and_previous_keys() -> None:
    assert _kms_vars_crypto_reads() == {
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        "BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64",
    }


@pytest.mark.parametrize("service", ["backend", "worker"])
@pytest.mark.parametrize("name", sorted(_kms_vars_crypto_reads()))
def test_the_kms_variable_reaches_the_service(service: str, name: str) -> None:
    assert name in _env(service), f"{name} never reaches the {service} container"


@pytest.mark.parametrize("service", ["backend", "worker"])
def test_previous_keys_are_optional(service: str) -> None:
    value = _env(service)["BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64"]

    assert value == "${BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64:-}"
