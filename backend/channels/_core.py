"""Channel — a declared, typed producer→consumer coupling (INV-1).

A :class:`Channel` is *not* a persistence layer. It is a declaration plus a
guard wrapper over the repository seam that already writes/reads the row. It
exists so that producer→consumer coupling is a **typed object** — declared
producer and consumer ids — rather than a bare table name that no tool can
see. An orphaned half (a producer nobody consumes, a consumer with no
producer) becomes a build failure via the meta-tests in
``tests/architecture/test_channel_registry.py``.

Two rules keep the abstraction honest:

* ``emit`` / ``consume`` take the **repository seam** (or the SQLAlchemy
  session, which structurally satisfies :class:`SupportsAdd`), never a raw
  transaction. The channel only ``add``s a row or delegates a claim — it
  never opens, flushes, or commits a transaction. The runner keeps owning
  the transaction boundary (batch rollback stays intact).
* Every write asserts its ``producer_id`` is declared; every read asserts
  its ``consumer_id`` is declared. An undeclared id raises rather than
  silently coupling through a string.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

TRow = TypeVar("TRow")
TRow_contra = TypeVar("TRow_contra", contravariant=True)


class UndeclaredProducerError(RuntimeError):
    """A write was attempted with a ``producer_id`` the channel does not declare."""


class UndeclaredConsumerError(RuntimeError):
    """A read was attempted with a ``consumer_id`` the channel does not declare."""


class SupportsAdd(Protocol[TRow_contra]):
    """The write half of the repository seam a channel guards.

    A SQLAlchemy ``AsyncSession`` satisfies this structurally (it has a
    synchronous ``add``), so a producer may pass either its repository or
    the session directly.
    """

    def add(self, row: TRow_contra) -> None: ...


@dataclass(frozen=True)
class Channel(Generic[TRow]):
    """A declared producer→consumer coupling over a single row type."""

    name: str
    row: type[TRow]
    producers: tuple[str, ...]
    consumers: tuple[str, ...]
    human_origin: bool = False
    authoring_surface: str | None = None

    def assert_producer(self, producer_id: str) -> None:
        if producer_id not in self.producers:
            raise UndeclaredProducerError(
                f"{producer_id!r} is not a declared producer of channel "
                f"{self.name!r} (declared: {self.producers})"
            )

    def assert_consumer(self, consumer_id: str) -> None:
        if consumer_id not in self.consumers:
            raise UndeclaredConsumerError(
                f"{consumer_id!r} is not a declared consumer of channel "
                f"{self.name!r} (declared: {self.consumers})"
            )

    def emit(self, repo: SupportsAdd[TRow], row: TRow, *, producer_id: str) -> None:
        """Stage ``row`` for insert through ``repo``, after asserting the producer.

        Synchronous and transaction-free: it only ``add``s. The caller (or
        the repository method wrapping this) owns any flush/commit.
        """
        self.assert_producer(producer_id)
        repo.add(row)

    async def consume(
        self,
        *,
        consumer_id: str,
        claim: Callable[[], Awaitable[list[TRow]]],
    ) -> list[TRow]:
        """Assert the consumer, then delegate to the repository's claim call.

        ``claim`` is the repository's existing claim method (the intake
        worker's is ``list_undrained``), passed as a thunk so the channel
        stays agnostic to each queue's claim shape while still gating the
        read behind a declared ``consumer_id``.
        """
        self.assert_consumer(consumer_id)
        return await claim()


__all__ = [
    "Channel",
    "SupportsAdd",
    "UndeclaredConsumerError",
    "UndeclaredProducerError",
]
