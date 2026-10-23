"""Every KMS variable the crypto layer reads must reach backend AND worker.

#957 added ``BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64`` so the KMS key can rotate, but a
variable only ``.env.prod`` knows about never reaches the container: compose passes an
explicit allow-list. Without it the rotation runbook's step 2 silently does nothing and
the first rewrapped-but-unreadable row fails loudly in prod.

The set is counted from ``crypto.py`` itself, so a new KMS variable cannot be added there
without this test demanding it in compose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CRYPTO = _REPO / "backend" / "router" / "accounts" / "crypto.py"
_COMPOSE = _REPO / "deploy" / "compose.prod.yaml"

_SERVICES = ("backend", "worker")
_NEXT_SERVICES = ("postgres", "redis", "backend", "worker", "pwa", "appdata")


def _kms_vars_read_by_crypto() -> set[str]:
    return set(re.findall(r"BSVIBE_GATEWAY_KMS_[A-Z0-9_]+", _CRYPTO.read_text("utf-8")))


def _service_block(service: str) -> str:
    text = _COMPOSE.read_text(encoding="utf-8")
    start = text.index(f"\n  {service}:\n") + 1
    rest = text[start + 1 :]
    ends = [
        rest.index(f"\n  {name}:\n")
        for name in _NEXT_SERVICES
        if name != service and f"\n  {name}:\n" in rest
    ]
    return rest[: min(ends)] if ends else rest


def test_crypto_reads_the_variables_we_expect() -> None:
    # Guards the guard: a regex that matches nothing would make the rest vacuous.
    assert _kms_vars_read_by_crypto() >= {
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        "BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64",
    }


@pytest.mark.parametrize("service", _SERVICES)
@pytest.mark.parametrize("name", sorted(_kms_vars_read_by_crypto()))
def test_the_container_receives_the_kms_variable_crypto_reads(service: str, name: str) -> None:
    assert re.search(rf"^\s+{name}:", _service_block(service), re.MULTILINE), (
        f"{name} is read by backend/router/accounts/crypto.py but compose.prod.yaml does "
        f"not pass it to {service}; setting it in .env.prod would have no effect."
    )
