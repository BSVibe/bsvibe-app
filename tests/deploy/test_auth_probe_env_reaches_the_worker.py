"""The auth probe must run in a process that HAS the auth config.

Shipped 2026-08-28 (#841) and it paged the founder within seconds of deploy —
falsely. ``AuthDependencyWorker`` runs in the WORKER container, and only the
BACKEND service declared ``USER_JWT_*``. With none of it present the settings
fall back to HS256-with-no-secret, the probe honestly reports "unconfigured",
and the worker read that as an outage. Three false "Sign-in is down" alerts
went out, two of them to telegram.

Two guards, because either alone still fails:

* Without the passthrough the probe cannot SEE the JWKS url, so it can never
  observe the real outage — the detector is inert, which is worse than absent
  because it looks present.
* Without the worker-side rule any future process that lacks the env pages
  again (:mod:`tests.notifications.test_auth_dependency_worker`).

The compose comment above the backend's block already spelled this out —
"Without these the verifier defaults to HS256 + a missing user_jwt_secret" —
which is verbatim what the false alert said. The knowledge was in the file; the
guard was not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO / "deploy"

#: What the probe needs to say anything true about the deployment's key source.
_AUTH_ENV = ("USER_JWT_JWKS_URL", "USER_JWT_ISSUER", "USER_JWT_ALGORITHM")


@pytest.fixture(scope="module")
def worker_env_block() -> str:
    """Just the ``worker:`` service block of compose.prod.yaml."""
    text = (_DEPLOY / "compose.prod.yaml").read_text(encoding="utf-8")
    start = text.index("\n  worker:")
    rest = text[start + 1 :]
    # The next top-level service key ends the block.
    ends = [
        rest.index(f"\n  {name}:")
        for name in ("pwa", "appdata", "postgres", "redis", "backend")
        if f"\n  {name}:" in rest
    ]
    return rest[: min(ends)] if ends else rest


@pytest.mark.parametrize("name", _AUTH_ENV)
def test_the_worker_receives_the_auth_config_its_probe_reads(
    worker_env_block: str, name: str
) -> None:
    assert name in worker_env_block, (
        f"{name} never reaches the worker container, so AuthDependencyWorker's "
        "probe cannot observe the real dependency — it can only report "
        "'unconfigured', which is a false alarm, not a detection."
    )
