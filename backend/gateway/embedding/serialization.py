"""Pack / unpack ``list[float]`` to packed-float32 bytes.

Used by :class:`backend.gateway.embedding.column.EmbeddingVector` for the
SQLite test path, and as a general-purpose serializer for places that
want a stable byte form (e.g. caches, audit logs).
"""

from __future__ import annotations

import struct


def serialize_embedding(vec: list[float]) -> bytes:
    if not vec:
        return b""
    return struct.pack(f"{len(vec)}f", *vec)


def deserialize_embedding(data: bytes) -> list[float]:
    # Truncate trailing partial bytes — packed float32 means 4-byte boundary.
    count = len(data) // 4
    if count == 0:
        return []
    return list(struct.unpack(f"{count}f", data[: count * 4]))
