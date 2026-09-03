"""``.env.prod.example`` must describe the deploy the compose files actually run.

The template says *"Copy to deploy/.env.prod and fill in real values"*, so it is
the ONLY thing a fresh production deploy has to go on. Nothing checked that it
still matched, and it had drifted in both directions (measured 2026-09-03):

* it documented ``BSVIBE_DEFAULT_WORKSPACE_REGION``, whose axis was **deleted**
  by the ``drop_workspace_region`` migration on 2026-08-28 — no code reads it,
  and ``compose.prod.yaml`` still forwarded it to two services, and
* it did NOT document ``BSVIBE_PRODUCT_BUNDLE_*``. That one is the dangerous
  direction: compose defaults the backend to ``local``
  (``${BSVIBE_PRODUCT_BUNDLE_BACKEND:-local}``), so a deploy built from this
  template comes up **healthy and silently non-durable** — product bundles back
  on the app disk, which is the failure this product treats as an unrecoverable
  brick. A missing REQUIRED var is loud; a missing OPTIONAL one is silent, and
  the silent ones are why this file exists.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.config import Settings

_DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
_TEMPLATE = _DEPLOY / ".env.prod.example"
_COMPOSE_PROD = _DEPLOY / "compose.prod.yaml"

#: Names compose interpolates that are NOT application settings. Each is a
#: compose-level input feeding some OTHER value, so it has no ``Settings``
#: field by design — listing them here keeps the mapping check honest instead
#: of turning it into "warn about anything unfamiliar".
_NOT_A_SETTING = {
    "BSVIBE_DB_PASSWORD": "feeds POSTGRES_PASSWORD + the DSNs, never read directly",
    "BSVIBE_APP_DB_PASSWORD": "the least-privilege app role's half of the same",
    "BSVIBE_REDIS_PASSWORD": "feeds the redis command line AND the URL (single source)",
}

#: Vars whose compose DEFAULT quietly changes production posture. A deploy that
#: omits them starts fine and is wrong — so the template has to name them even
#: though nothing fails without them. Curated on purpose: it cannot be derived,
#: because "has a default" is exactly what makes these dangerous.
_SILENT_IF_OMITTED = {
    "BSVIBE_PRODUCT_BUNDLE_BACKEND": "defaults to `local` — bundles back on the app disk (not durable)",
    "BSVIBE_PRODUCT_BUNDLE_S3_ENDPOINT": "the R2 endpoint the `s3` backend needs",
    "BSVIBE_PRODUCT_BUNDLE_S3_BUCKET": "as above",
    "BSVIBE_PRODUCT_BUNDLE_S3_ACCESS_KEY": "as above",
    "BSVIBE_PRODUCT_BUNDLE_S3_SECRET_KEY": "as above",
    "BSVIBE_OAUTH_ISSUER": "defaults to this deploy's own hostname; wrong issuer = tokens no one accepts",
}


def _template_text() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def _compose_text() -> str:
    return _COMPOSE_PROD.read_text(encoding="utf-8")


def _documented() -> set[str]:
    """Names the template assigns — what a founder filling it in would set."""
    return set(re.findall(r"^([A-Z][A-Z0-9_]+)=", _template_text(), re.M))


def _interpolated() -> dict[str, bool]:
    """``{name: has_a_default}`` for every ``${VAR}`` compose reads from the env.

    A name is only "required" when NO occurrence supplies a default — one
    ``${VAR:-x}`` anywhere makes the deploy survive without it.
    """
    seen: dict[str, bool] = {}
    for match in re.finditer(r"\$\{([A-Z][A-Z0-9_]+)(:?[-?])?", _compose_text()):
        name, operator = match.group(1), match.group(2)
        has_default = operator in (":-", "-")
        seen[name] = seen.get(name, False) or has_default
    return seen


def test_every_bsvibe_name_maps_to_a_real_setting() -> None:
    """A name nothing reads is not a knob — it is a leftover that lies.

    Catches the case where the code behind a setting is deleted but its env
    plumbing survives: the template keeps offering it, compose keeps forwarding
    it, and setting it does nothing at all.
    """
    fields = set(Settings.model_fields)
    names = _documented() | set(_interpolated())
    orphans = sorted(
        name
        for name in names
        if name.startswith("BSVIBE_")
        and name not in _NOT_A_SETTING
        and name[len("BSVIBE_") :].lower() not in fields
    )

    assert orphans == [], (
        f"{orphans} appear in the deploy config but map to no `Settings` field. "
        "Either the setting was removed and this is leftover plumbing (delete it "
        "from .env.prod.example AND compose.prod.yaml), or it is a compose-level "
        "input that belongs in _NOT_A_SETTING with a reason."
    )


def test_the_template_documents_everything_a_deploy_must_supply() -> None:
    """Vars compose interpolates WITHOUT a default — `up` fails without them."""
    required = {name for name, has_default in _interpolated().items() if not has_default}
    missing = sorted(required - _documented())

    assert missing == [], (
        f"{missing} are required by compose.prod.yaml (no default) but absent from "
        f"{_TEMPLATE.name}. A deploy built from the template cannot start."
    )


@pytest.mark.parametrize(("name", "why"), sorted(_SILENT_IF_OMITTED.items()))
def test_the_template_documents_what_is_silently_wrong_when_omitted(name: str, why: str) -> None:
    """The dangerous half: compose HAS a default, so omitting it is not an error.

    These are the ones a template can lose without anyone noticing — the deploy
    is green and the property is gone.
    """
    assert name in _documented(), (
        f"{name} is missing from {_TEMPLATE.name}, and compose.prod.yaml supplies "
        f"a default for it — so a deploy without it starts healthy and is wrong: {why}."
    )


def test_the_silent_list_still_describes_this_compose_file() -> None:
    """Pin the pin. If a var stops being defaulted (or disappears), say so here.

    Without this the curated list above rots into a set of names that no longer
    correspond to anything, and it keeps passing.
    """
    interpolated = _interpolated()
    stale = sorted(name for name in _SILENT_IF_OMITTED if not interpolated.get(name, False))

    assert stale == [], (
        f"{stale} no longer have a compose default (or are gone from "
        "compose.prod.yaml). Re-check whether each still belongs in "
        "_SILENT_IF_OMITTED — the reason it was listed has changed."
    )
