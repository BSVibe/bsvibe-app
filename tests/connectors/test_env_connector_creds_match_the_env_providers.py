"""A connector gets env credentials iff it is actually registered from env.

``.env.example`` promises, in prose: *"the connector offers 'Connect with X' only
when both id + secret are set."* For sentry that was **false**. Sentry is a
DB-credential provider (``bootstrap._DB_PROVIDERS``, install→grant), never built
from settings — the same as github, which correctly has no runtime env var. So
``BSVIBE_SENTRY_CLIENT_ID`` could be filled in and nothing would happen, with no
error and no way to find out why.

The settings existed because the env vars predate sentry's DB wiring. Nothing
read them: measured across code, tests, deploy, CI, docs and the PWA, the only
occurrence of ``sentry_client_id`` in the repo was its own definition.

These guards pin the RESULT SET on both surfaces rather than naming sentry, so a
future provider that drifts the same way fails here too — in either direction:
an env var with no env wiring, or env wiring with no documented var.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.config import Settings
from backend.connectors.auth.bootstrap import _DB_PROVIDERS, _ENV_PROVIDERS

_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _env_wired() -> set[str]:
    return {name for name, _build, _id_attr, _secret_attr in _ENV_PROVIDERS}


def _settings_cred_providers() -> set[str]:
    return {
        m.group(1)
        for field in Settings.model_fields
        if (m := re.fullmatch(r"([a-z0-9]+)_client_id", field))
    }


def _env_example_cred_providers() -> set[str]:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    return {m.lower() for m in re.findall(r"^BSVIBE_([A-Z0-9]+)_CLIENT_ID=", text, flags=re.M)}


def test_settings_carry_client_creds_for_exactly_the_env_registered_providers() -> None:
    """A ``*_client_id`` setting nothing builds a provider from is inert config."""
    assert _settings_cred_providers() == _env_wired()


def test_env_example_offers_client_creds_for_exactly_those_providers() -> None:
    """The file founders actually read must not offer a knob that does nothing."""
    assert _env_example_cred_providers() == _env_wired()


def test_db_credential_providers_have_no_env_knob() -> None:
    """Control, stated as the rule rather than as a list: a provider whose creds
    live in the DB must expose no env credential on EITHER surface.

    github already obeyed this (``.env.example`` says so in prose: *"There is no
    BSVIBE_GITHUB_APP_* runtime var to set."*); sentry did not.
    """
    db_only = set(_DB_PROVIDERS) - _env_wired()
    assert db_only, "no DB-only provider left — this control would pass vacuously"
    assert db_only & _settings_cred_providers() == set()
    assert db_only & _env_example_cred_providers() == set()
