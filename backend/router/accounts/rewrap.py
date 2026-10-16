"""Re-encrypt every stored secret under the current key — the last step of a key rotation (#957).

Usage (as the OWNER role — ``BSVIBE_MIGRATION_DATABASE_URL`` — on the host that runs prod)::

    python -m backend.router.accounts.rewrap            # dry run: what would change
    python -m backend.router.accounts.rewrap --apply    # rewrite

Exit code 0 = every stored secret now opens with the current key alone; 1 = at least one value
opens with NO configured key (reported by table.column and row key — never the value). Drop the
old key from ``BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64`` only after an ``--apply`` run exits 0.

The inventory (:data:`ENCRYPTED_COLUMNS` + the ``products.metadata`` sealed secrets) is pinned
against every mapped ``*_ciphertext`` / ``*_encrypted`` column by a test, so a new encrypted
column cannot be added without this command learning about it. Tables are named, not imported:
this tool sits in the router context and must not import the connector or workflow models.

Each row is compare-and-set (``WHERE <col> = <old>``), so a value rewritten concurrently by the
app is skipped rather than clobbered. ``products`` is RLS-forced: it is read across tenants and
each row written inside its own workspace's scope, which works for the owner and app roles alike.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
import structlog
from cryptography.exceptions import InvalidTag
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.data.rls import cross_tenant_session_read, workspace_session_scope
from backend.router.accounts.crypto import (
    CredentialCipher,
    UnknownCredentialKey,
    cipher_from_settings,
)

logger = structlog.get_logger(__name__)

#: Sealed product secrets: ``products.metadata["verify_secrets"][NAME] = "enc:" + ciphertext``
#: (``backend.workflow.domain.verify_secrets`` — mirrored here, not imported, for the reason
#: the module docstring gives).
_SEALED_PREFIX = "enc:"
_VERIFY_SECRETS_KEY = "verify_secrets"


@dataclass(frozen=True)
class EncryptedTable:
    table: str
    key: str
    columns: tuple[str, ...]
    key_is_uuid: bool = True


ENCRYPTED_COLUMNS: tuple[EncryptedTable, ...] = (
    EncryptedTable("connector_accounts", "id", ("signing_secret_ciphertext",)),
    EncryptedTable(
        "connector_oauth_tokens", "id", ("access_token_ciphertext", "refresh_token_ciphertext")
    ),
    EncryptedTable(
        "connector_oauth_app_credentials",
        "provider",
        ("client_secret_ciphertext", "private_key_pem_ciphertext", "webhook_secret_ciphertext"),
        key_is_uuid=False,
    ),
    EncryptedTable(
        "connector_oauth_unclaimed", "id", ("access_token_ciphertext", "refresh_token_ciphertext")
    ),
    EncryptedTable("model_accounts", "id", ("api_key_encrypted",)),
)


@dataclass
class RewrapReport:
    current: int = 0
    would_rewrap: int = 0
    rewrapped: int = 0
    undecryptable: int = 0
    failures: list[str] = field(default_factory=list)


def _rewrapped(
    cipher: CredentialCipher, token: str, where: str, report: RewrapReport
) -> str | None:
    """The value re-encrypted under the current key, ``None`` when it needs nothing / opens with
    nothing (counted in ``report`` either way)."""
    if not cipher.needs_rewrap(token):
        report.current += 1
        return None
    try:
        plaintext = cipher.decrypt(token)
    except (UnknownCredentialKey, InvalidTag, ValueError):
        report.undecryptable += 1
        report.failures.append(where)
        logger.error("credential_rewrap_undecryptable", where=where)
        return None
    report.would_rewrap += 1
    return cipher.encrypt(plaintext)


async def _rewrap_table(
    session: AsyncSession,
    spec: EncryptedTable,
    cipher: CredentialCipher,
    *,
    apply: bool,
    report: RewrapReport,
) -> None:
    key_type: Any = sa.Uuid if spec.key_is_uuid else sa.String
    table = sa.table(
        spec.table, sa.column(spec.key, key_type), *(sa.column(c) for c in spec.columns)
    )
    key = table.c[spec.key]
    for column in spec.columns:
        col = table.c[column]
        rows = (await session.execute(sa.select(key, col).where(col.is_not(None)))).all()
        for row_key, token in rows:
            new = _rewrapped(cipher, token, f"{spec.table}.{column}[{row_key}]", report)
            if new is None or not apply:
                continue
            result = await session.execute(
                sa.update(table).where(key == row_key, col == token).values({column: new})
            )
            report.rewrapped += result.rowcount  # type: ignore[attr-defined]


async def _rewrap_products(
    session: AsyncSession, cipher: CredentialCipher, *, apply: bool, report: RewrapReport
) -> None:
    products = sa.table(
        "products",
        sa.column("id", sa.Uuid),
        sa.column("workspace_id", sa.Uuid),
        sa.column("metadata", sa.JSON),
    )
    async with cross_tenant_session_read(session):
        rows = (
            await session.execute(
                sa.select(products.c.id, products.c.workspace_id, products.c.metadata)
            )
        ).all()
    for product_id, workspace_id, metadata in rows:
        secrets = (metadata or {}).get(_VERIFY_SECRETS_KEY)
        if not isinstance(secrets, dict):
            continue
        changed = dict(secrets)
        for name, value in secrets.items():
            if not (isinstance(value, str) and value.startswith(_SEALED_PREFIX)):
                continue
            where = f"products.metadata.{_VERIFY_SECRETS_KEY}.{name}[{product_id}]"
            new = _rewrapped(cipher, value[len(_SEALED_PREFIX) :], where, report)
            if new is not None:
                changed[name] = _SEALED_PREFIX + new
        if not apply or changed == secrets:
            continue
        async with workspace_session_scope(session, uuid.UUID(str(workspace_id))):
            await session.execute(
                sa.update(products)
                .where(products.c.id == product_id)
                .values(metadata={**metadata, _VERIFY_SECRETS_KEY: changed})
            )
        report.rewrapped += sum(1 for n in changed if changed[n] != secrets[n])


async def rewrap(
    session_factory: async_sessionmaker[AsyncSession], cipher: CredentialCipher, *, apply: bool
) -> RewrapReport:
    """Move every stored secret onto ``cipher``'s current key (``apply``) or report it (dry run)."""
    report = RewrapReport()
    async with session_factory() as session:
        for spec in ENCRYPTED_COLUMNS:
            await _rewrap_table(session, spec, cipher, apply=apply, report=report)
        await _rewrap_products(session, cipher, apply=apply, report=report)
        if apply:
            await session.commit()
    logger.info(
        "credential_rewrap_done",
        apply=apply,
        current=report.current,
        would_rewrap=report.would_rewrap,
        rewrapped=report.rewrapped,
        undecryptable=report.undecryptable,
    )
    return report


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rewrap",
        description="Re-encrypt every stored secret under the current KMS key (#957).",
    )
    parser.add_argument("--apply", action="store_true", help="Rewrite (default: dry run).")
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    from backend.config import get_settings  # noqa: PLC0415

    engine = create_async_engine(get_settings().migration_url(), future=True)
    try:
        report = await rewrap(
            async_sessionmaker(engine, expire_on_commit=False),
            cipher_from_settings(),
            apply=bool(args.apply),
        )
    finally:
        await engine.dispose()
    verb = "rewrapped" if args.apply else "would rewrap"
    count = report.rewrapped if args.apply else report.would_rewrap
    print(f"current {report.current} · {verb} {count} · undecryptable {report.undecryptable}")
    for where in report.failures:
        print(f"  UNDECRYPTABLE {where}")
    return 1 if report.undecryptable else 0


def main(argv: Sequence[str] | None = None) -> int:
    from backend.shared.core.logging import configure_logging  # noqa: PLC0415

    configure_logging(level="INFO", service_name="bsvibe-credential-rewrap")
    return asyncio.run(_main_async(_parse_args(argv if argv is not None else sys.argv[1:])))


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["ENCRYPTED_COLUMNS", "EncryptedTable", "RewrapReport", "main", "rewrap"]
