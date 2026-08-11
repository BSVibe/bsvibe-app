"""Both write seams seal, both read seams mask — or neither is safe.

A secret sealed on the REST path and stored in the clear on the MCP path is a
secret in the clear; a product read through one endpoint that masks and another
that does not is a secret published. So these are asserted per-seam rather than
per-helper: the helper is already covered as a unit, and what breaks in practice
is a new endpoint that forgot to call it.
"""

from __future__ import annotations

import uuid

import pytest

from backend.workflow.domain.verify_secrets import MASK, METADATA_KEY

pytestmark = pytest.mark.asyncio

_SECRET = "hunter2-do-not-leak"  # noqa: S105 — the string these tests hunt for
_KEY = b"0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def kms_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the seam THIS file's key, without touching settings.

    The obvious version — set ``BSVIBE_GATEWAY_KMS_KEY_B64`` and
    ``get_settings.cache_clear()`` — pollutes the whole session twice over: the
    cache is global, so clearing it makes every later reader rebuild settings
    from whatever the environment happens to be at that moment, and a fixture
    requesting ``monkeypatch`` is finalised BEFORE it, so the clear re-caches
    this test's key. Both were observed: unrelated glue and alembic tests
    failing in the full run and passing in isolation, with a different set each
    time.

    Patching the key FUNCTION instead touches nothing global.
    """
    monkeypatch.setattr(
        "backend.workflow.application.product_secrets._key_from_settings",
        lambda: _KEY,
        raising=True,
    )
    monkeypatch.setattr(
        "backend.router.accounts.crypto._key_from_settings", lambda: _KEY, raising=True
    )


async def test_the_rest_seam_seals_and_masks() -> None:
    from backend.api.v1.products._schemas import ProductResponse
    from backend.workflow.application.product_secrets import sealed_product_metadata

    stored = sealed_product_metadata({METADATA_KEY: {"K": _SECRET}}, {})

    assert _SECRET not in str(stored), "the plaintext reached the column"

    shown = ProductResponse.model_validate(
        {
            "id": uuid.uuid4(),
            "workspace_id": uuid.uuid4(),
            "slug": "p",
            "name": "p",
            "repo_url": None,
            "bootstrap_status": None,
            "bootstrap_error": None,
            "bootstrap_progress": None,
            "product_metadata": stored,
            "created_at": "2026-08-11T00:00:00+00:00",
            "updated_at": "2026-08-11T00:00:00+00:00",
        }
    )

    assert shown.metadata[METADATA_KEY] == {"K": MASK}
    assert stored[METADATA_KEY]["K"] not in shown.model_dump_json(), (
        "the ciphertext left through the API — the response model is the one "
        "shape every reader gets, so masking has to happen there"
    )


async def test_the_round_trip_does_not_wipe_the_secret() -> None:
    """Read the product, edit something unrelated, write the whole object back.

    This is what a settings screen does, and taking the mask it was shown
    literally would destroy the credential on every save.
    """
    from backend.workflow.application.product_secrets import sealed_product_metadata
    from backend.workflow.domain.verify_secrets import redact_secrets, unseal_secrets

    stored = sealed_product_metadata({METADATA_KEY: {"K": _SECRET}}, {})
    as_read = redact_secrets(stored)
    as_read["execution_target"] = "client_attach"

    written_back = sealed_product_metadata(as_read, stored)

    from backend.router.accounts.crypto import CredentialCipher, _key_from_settings

    cipher = CredentialCipher(_key_from_settings())
    assert unseal_secrets(written_back, decrypt=cipher.decrypt) == {"K": _SECRET}
    assert written_back["execution_target"] == "client_attach"
