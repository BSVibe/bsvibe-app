"""serialize_embedding / deserialize_embedding round-trip."""

from __future__ import annotations

import struct

from backend.gateway.embedding.serialization import (
    deserialize_embedding,
    serialize_embedding,
)


class TestSerialization:
    def test_round_trip_preserves_values(self):
        vec = [0.1, -0.2, 3.14, 1e-6, 0.0]
        out = deserialize_embedding(serialize_embedding(vec))
        # float32 precision means we get small error.
        for a, b in zip(vec, out, strict=True):
            assert abs(a - b) < 1e-5

    def test_empty_vector(self):
        assert serialize_embedding([]) == b""
        assert deserialize_embedding(b"") == []

    def test_byte_length_matches_dim(self):
        # 4 bytes per float32.
        assert len(serialize_embedding([1.0] * 768)) == 768 * 4

    def test_struct_format_is_float32(self):
        # Sanity — first 4 bytes equal struct.pack("f", value).
        vec = [1.5]
        assert serialize_embedding(vec) == struct.pack("f", 1.5)


class TestDeserializeMalformed:
    def test_partial_bytes_truncate(self):
        # 5 bytes is not a multiple of 4 — drop the trailing byte rather than crash.
        partial = struct.pack("f", 1.0) + b"\x00"
        out = deserialize_embedding(partial)
        assert len(out) == 1
        assert abs(out[0] - 1.0) < 1e-5
