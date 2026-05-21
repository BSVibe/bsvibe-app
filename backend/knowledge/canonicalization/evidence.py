"""Evidence envelope helpers (Handoff §2 Evidence envelope).

Single source of truth for the typed evidence envelope shape used across
service Hard Blocks, proposal generation, scoring risk_reasons, and
ingest provenance.

Per Handoff §2:
- ``source`` MUST be one of ``deterministic`` / ``model`` / ``human`` / ``system``.
- Deterministic risk_reasons MUST NOT be derived from model evidence (§13).
- Each item carries ``schema_version`` so consumers can branch on shape.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

EvidenceSource = Literal["deterministic", "model", "human", "system"]


def envelope(
    *,
    kind: str,
    schema_version: str,
    payload: dict[str, Any],
    source: EvidenceSource = "deterministic",
    producer: str = "canonicalization",
    observed_at: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Build an Evidence envelope dict (Handoff §2).

    Either ``observed_at`` (absolute) or ``clock`` (callable) may be
    provided; if both are None the wall clock in UTC is used.
    """
    if observed_at is not None:
        ts = observed_at
    elif clock is not None:
        ts = clock()
    else:
        ts = datetime.now(tz=UTC)
    return {
        "kind": kind,
        "schema_version": schema_version,
        "source": source,
        "observed_at": ts.isoformat(),
        "producer": producer,
        "payload": payload,
    }


def hard_block(
    reason: str,
    *,
    producer: str = "canonicalization.service-v1",
    clock: Callable[[], datetime] | None = None,
    **payload: Any,
) -> dict[str, Any]:
    """Convenience wrapper for the deterministic Hard Block envelopes used
    across the service validate path.

    Mirrors the pre-consolidation ``_evidence(reason, **payload)`` helper.
    """
    return envelope(
        kind="deterministic_check",
        schema_version="deterministic-check-v1",
        payload={"reason": reason, **payload},
        source="deterministic",
        producer=producer,
        clock=clock,
    )
