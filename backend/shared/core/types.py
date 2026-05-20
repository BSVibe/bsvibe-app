"""Type aliases shared by every BSVibe Python package.

Resist the temptation to introduce ``NewType`` here — products freely
mix string IDs across boundaries and a strict ``NewType`` would force
casts in every adapter. Aliases keep static checks honest without
runtime overhead.
"""

from __future__ import annotations

from typing import Any, TypeAlias

#: Tenant identifier (UUID string).
TenantId: TypeAlias = str

#: User identifier (UUID string from BSVibe-Auth).
UserId: TypeAlias = str

#: Request correlation identifier (free-form, request-scoped).
RequestId: TypeAlias = str

#: A single JSON value (recursive — runtime is loose).
JsonValue: TypeAlias = Any

#: A JSON object — keys are always strings.
JsonDict: TypeAlias = dict[str, JsonValue]

__all__ = [
    "TenantId",
    "UserId",
    "RequestId",
    "JsonValue",
    "JsonDict",
]
