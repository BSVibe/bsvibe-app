"""Cross-dialect ``EmbeddingVector`` column type.

* PostgreSQL: ``vector`` (pgvector extension) — supports the ``<=>`` cosine
  distance operator and indexing once dim is locked per workspace.
* SQLite: ``BLOB`` storing packed float32. Used in the in-memory test
  conftest so we don't need a live Postgres for the broad test suite.

Both sides yield / accept ``list[float]`` to the ORM layer.
"""

from __future__ import annotations

from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import LargeBinary
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from backend.embedding.serialization import (
    deserialize_embedding,
    serialize_embedding,
)


class EmbeddingVector(TypeDecorator[list[float]]):
    """Variable-dimension vector. See module docstring for dialect behavior."""

    impl = LargeBinary
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            # Dim=None → ``vector`` with no fixed dimension. Indexing
            # requires a dim — see PR description "pgvector switch
            # criteria" for the path to a per-workspace fixed dim.
            return dialect.type_descriptor(Vector(None))
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(
        self, value: list[float] | None, dialect: Dialect
    ) -> list[float] | bytes | None:
        if value is None:
            return None
        if dialect.name == "postgresql":
            # pgvector's adapter handles ``list[float]`` directly.
            return value
        return serialize_embedding(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        if dialect.name == "postgresql":
            # pgvector returns list[float] (or numpy array — coerce).
            return list(value)
        return deserialize_embedding(value)
