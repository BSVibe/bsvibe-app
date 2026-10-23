"""Revoke every live OAuth access token — the last step of an OAuth signing-key rotation.

Usage (as the OWNER role — ``BSVIBE_MIGRATION_DATABASE_URL`` — inside the backend container)::

    python -m backend.identity.revoke_access_tokens            # dry run: how many are live
    python -m backend.identity.revoke_access_tokens --apply    # revoke them

Swapping ``BSVIBE_OAUTH_PRIVATE_KEY_PEM`` alone is not enough: a refresh token is stored only as a
hash, but clients hold the plaintext, and a refresh returns a token signed by the NEW key. Revoking
the parent access tokens is what makes a refresh fail with ``PARENT_REVOKED``.

Idempotent: only rows with ``revoked_at IS NULL`` are touched, so a re-run revokes nothing and
keeps the original revocation timestamps. Exit code 0 = no live access token remains (after
``--apply``) / the count was reported (dry run).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = structlog.get_logger(__name__)

# Named, not imported: this is an operator tool and must not drag in the ORM model graph.
_TOKENS = sa.table("oauth_access_tokens", sa.column("revoked_at", sa.DateTime(timezone=True)))


async def revoke_access_tokens(
    session_factory: async_sessionmaker[AsyncSession], *, apply: bool, now: datetime | None = None
) -> int:
    """Count (dry run) or revoke (``apply``) every access token with ``revoked_at IS NULL``."""
    async with session_factory() as session:
        if not apply:
            live = await session.scalar(
                sa.select(sa.func.count())
                .select_from(_TOKENS)
                .where(_TOKENS.c.revoked_at.is_(None))
            )
            count = int(live or 0)
        else:
            result = await session.execute(
                sa.update(_TOKENS)
                .where(_TOKENS.c.revoked_at.is_(None))
                .values(revoked_at=now or datetime.now(UTC))
            )
            count = result.rowcount  # type: ignore[attr-defined]
            await session.commit()
    logger.info("oauth_access_tokens_revoked", apply=apply, count=count)
    return count


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="revoke_access_tokens",
        description="Revoke every live OAuth access token (signing-key rotation).",
    )
    parser.add_argument("--apply", action="store_true", help="Revoke (default: dry run).")
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    from backend.config import get_settings  # noqa: PLC0415

    engine = create_async_engine(get_settings().migration_url(), future=True)
    try:
        count = await revoke_access_tokens(
            async_sessionmaker(engine, expire_on_commit=False), apply=bool(args.apply)
        )
    finally:
        await engine.dispose()
    print(f"{'revoked' if args.apply else 'would revoke'} {count} live access token(s)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from backend.shared.core.logging import configure_logging  # noqa: PLC0415

    configure_logging(level="INFO", service_name="bsvibe-revoke-access-tokens")
    return asyncio.run(_main_async(_parse_args(argv if argv is not None else sys.argv[1:])))


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "revoke_access_tokens"]
