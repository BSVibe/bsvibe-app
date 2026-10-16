"""#957 — the rewrap command moves every stored secret onto the current key.

Rotation is: new key current, old key in ``BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64``, deploy, run
``python -m backend.router.accounts.rewrap --apply``, drop the old key. The last step is safe
only if the command reached EVERY stored secret — a column it skipped becomes unreadable the
moment the old key is dropped, silently, until something next decrypts it. So:

* every value not already ``kms1.<current kid>`` is re-encrypted; the plaintext is unchanged;
* a dry run (the default) changes nothing and reports what it would do;
* a value no configured key opens is LEFT ALONE and REPORTED (the exit code says so) — the
  operator must not drop the old key while that count is non-zero;
* re-running is a no-op;
* ``products.metadata.verify_secrets`` ``enc:`` values are rewrapped too (they live inside JSON);
* the inventory is pinned against every mapped ``*_ciphertext`` / ``*_encrypted`` column, so a
  new encrypted column cannot be added without the command learning about it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.connectors.auth.db  # noqa: F401
import backend.connectors.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.models  # noqa: F401
from backend.connectors.db import ConnectorAccountRow
from backend.data import Base
from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.router.accounts.crypto import CredentialCipher, key_id
from backend.router.accounts.models import ModelAccount
from backend.router.accounts.rewrap import ENCRYPTED_COLUMNS, rewrap

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_OLD = b"\x01" * 32
_NEW = b"\x02" * 32
_LOST = b"\x03" * 32


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(sf) -> dict[str, Any]:
    """One workspace holding: a bot token under the OLD key, an API key under the NEW key, a
    legacy-format API key, a value nobody can open, and a product with a sealed secret."""
    old, new = CredentialCipher(_OLD), CredentialCipher(_NEW)
    ws, now = uuid.uuid4(), datetime.now(UTC)
    ids: dict[str, Any] = {"ws": ws}
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(WorkspaceRow(id=ws, name="w", created_at=now, updated_at=now))
        await s.flush()
        bot = ConnectorAccountRow(
            workspace_id=ws,
            connector="telegram",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=old.encrypt("bot-token"),
            delivery_config={},
            is_active=True,
        )
        lost = ConnectorAccountRow(
            workspace_id=ws,
            connector="slack",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=CredentialCipher(_LOST).encrypt("gone"),
            delivery_config={},
            is_active=True,
        )
        current = ModelAccount(
            workspace_id=ws,
            account_id=uuid.uuid4(),
            provider="openai",
            label="current",
            litellm_model="openai/x",
            api_key_encrypted=new.encrypt("sk-current"),
            is_active=True,
            extra_params={},
        )
        legacy = ModelAccount(
            workspace_id=ws,
            account_id=uuid.uuid4(),
            provider="openai",
            label="legacy",
            litellm_model="openai/x",
            # The pre-#957 format: ciphertext body without the kms1.<kid>. prefix.
            api_key_encrypted=old.encrypt("sk-legacy").split(".", 2)[2],
            is_active=True,
            extra_params={},
        )
        product = ProductRow(
            workspace_id=ws,
            name="p",
            slug="p",
            created_at=now,
            updated_at=now,
            product_metadata={
                "stage": "beta",
                "verify_secrets": {"API_TOKEN": "enc:" + old.encrypt("prod-secret"), "EMPTY": ""},
            },
        )
        s.add_all([bot, lost, current, legacy, product])
        await s.commit()
        ids.update(
            bot=bot.id, lost=lost.id, current=current.id, legacy=legacy.id, product=product.id
        )
    return ids


async def _read(sf, ids) -> dict[str, Any]:
    async with sf() as s, workspace_session_scope(s, ids["ws"]):
        accounts = {r.id: r for r in (await s.execute(select(ConnectorAccountRow))).scalars()}
        models = {r.id: r for r in (await s.execute(select(ModelAccount))).scalars()}
        product = await s.get(ProductRow, ids["product"])
        assert product is not None
        return {
            "bot": accounts[ids["bot"]].signing_secret_ciphertext,
            "lost": accounts[ids["lost"]].signing_secret_ciphertext,
            "current": models[ids["current"]].api_key_encrypted,
            "legacy": models[ids["legacy"]].api_key_encrypted,
            "product": product.product_metadata,
        }


def _rotated() -> CredentialCipher:
    return CredentialCipher(_NEW, previous=[_OLD])


async def test_a_dry_run_changes_nothing_and_says_what_it_would_do(sf) -> None:
    ids = await _seed(sf)
    before = await _read(sf, ids)

    report = await rewrap(sf, _rotated(), apply=False)

    assert await _read(sf, ids) == before
    assert report.rewrapped == 0, "a dry run reported writes it must not make"
    assert report.would_rewrap == 3  # bot (old kid) · legacy model key · product secret
    assert report.current == 1
    assert report.undecryptable == 1


async def test_apply_moves_every_value_onto_the_current_key(sf) -> None:
    ids = await _seed(sf)

    report = await rewrap(sf, _rotated(), apply=True)
    after = await _read(sf, ids)

    kid = f"kms1.{key_id(_NEW)}."
    new_only = CredentialCipher(_NEW)  # the OLD key is gone after this — everything must open
    assert after["bot"].startswith(kid) and new_only.decrypt(after["bot"]) == "bot-token"
    assert after["legacy"].startswith(kid) and new_only.decrypt(after["legacy"]) == "sk-legacy"
    assert new_only.decrypt(after["current"]) == "sk-current"
    sealed = after["product"]["verify_secrets"]["API_TOKEN"]
    assert sealed.startswith("enc:" + kid)
    assert new_only.decrypt(sealed.removeprefix("enc:")) == "prod-secret"
    assert after["product"]["stage"] == "beta" and after["product"]["verify_secrets"]["EMPTY"] == ""
    assert report.rewrapped == 3 and report.undecryptable == 1


async def test_a_value_nothing_opens_is_left_alone_and_reported(sf) -> None:
    ids = await _seed(sf)
    before = await _read(sf, ids)

    report = await rewrap(sf, _rotated(), apply=True)

    assert (await _read(sf, ids))["lost"] == before["lost"]
    assert report.undecryptable == 1
    assert any("connector_accounts.signing_secret_ciphertext" in loc for loc in report.failures)


async def test_rerunning_is_a_no_op(sf) -> None:
    ids = await _seed(sf)
    await rewrap(sf, _rotated(), apply=True)
    once = await _read(sf, ids)

    report = await rewrap(sf, _rotated(), apply=True)

    assert await _read(sf, ids) == once
    assert report.rewrapped == 0 and report.current == 4


def test_the_inventory_covers_every_encrypted_column() -> None:
    """Ratchet: a new ``*_ciphertext`` / ``*_encrypted`` column must be added to the rewrap
    inventory, or rotating the key would silently strand it."""
    mapped = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.name.endswith(("_ciphertext", "_encrypted"))
    }
    covered = {(t.table, c) for t in ENCRYPTED_COLUMNS for c in t.columns}

    assert mapped == covered, f"not in the rewrap inventory: {sorted(mapped - covered)}"
