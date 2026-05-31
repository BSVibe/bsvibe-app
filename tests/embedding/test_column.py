"""EmbeddingVector TypeDecorator — SQLite round-trip + PG compile probe."""

from __future__ import annotations

from sqlalchemy import Column
from sqlalchemy.dialects import postgresql, sqlite

from backend.embedding.column import EmbeddingVector


class TestDialectCompile:
    def test_compiles_to_VECTOR_on_postgresql(self):
        col = Column("e", EmbeddingVector())
        ddl = col.type.dialect_impl(postgresql.dialect()).compile(postgresql.dialect())
        assert "VECTOR" in ddl.upper()

    def test_compiles_to_BLOB_on_sqlite(self):
        col = Column("e", EmbeddingVector())
        ddl = col.type.dialect_impl(sqlite.dialect()).compile(sqlite.dialect())
        # SQLite has no vector type — falls back to BLOB (LargeBinary).
        assert "BLOB" in ddl.upper()


class TestBindAndResultProcessing:
    def test_sqlite_packs_and_unpacks_float32(self):
        from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect_cls

        dialect = sqlite_dialect_cls()
        vec = [0.1, -0.2, 3.14]
        typ = EmbeddingVector()
        packed = typ.process_bind_param(vec, dialect)
        assert isinstance(packed, bytes)
        out = typ.process_result_value(packed, dialect)
        for a, b in zip(vec, out, strict=True):
            assert abs(a - b) < 1e-5

    def test_pg_passes_through(self):
        from sqlalchemy.dialects.postgresql import dialect as pg_dialect_cls

        dialect = pg_dialect_cls()
        vec = [0.1, -0.2]
        typ = EmbeddingVector()
        bound = typ.process_bind_param(vec, dialect)
        # On PG we hand the list straight to the pgvector adapter.
        assert bound == vec
        # Result path: pgvector returns list[float]; pass-through.
        assert typ.process_result_value([0.1, 0.2], dialect) == [0.1, 0.2]

    def test_none_round_trips(self):
        from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect_cls

        dialect = sqlite_dialect_cls()
        typ = EmbeddingVector()
        assert typ.process_bind_param(None, dialect) is None
        assert typ.process_result_value(None, dialect) is None
