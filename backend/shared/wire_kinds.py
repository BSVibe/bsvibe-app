"""Cross-context wire-contract vocabulary (neutral shared-kernel leaf).

A *wire contract* is a string one bounded context WRITES and another READS —
a schedule row's ``kind``, an inbound trigger payload's routing keys. Its
single definition belongs in neither context, because two spellings of one
contract is precisely how the two ends drift apart. So it lives here, in a
leaf module both may import without either depending on the other.

This module imports nothing from any bounded context (it satisfies the
"common leaves do not import bounded contexts" import-linter contract) and
is therefore also reachable from the MCP context, which may not import
:mod:`backend.schedule`.
"""

from __future__ import annotations

#: ``product_tick`` — an autonomous cadence tick: the founder sets only the
#: WHEN (per product), and BSVibe decides WHAT to do. Seeded by the schedule
#: emitter onto a run's ``payload["kind"]`` and read by the workflow workers
#: to route the resulting deliverable through Safe Mode.
SCHEDULE_KIND_PRODUCT_TICK = "product_tick"

#: The two keys a connector-inbound trigger payload carries so the Receive
#: stage (``backend.workflow.application.stages.intake.receive``) can resolve
#: it to its ``resource_bindings`` row — the same ``(connector_account_id,
#: resource_id)`` pair that table is indexed on.
#:
#: WRITTEN by the public webhook route (``backend.api.webhooks``) when — and
#: only when — it actually resolved a binding; READ by the Receive stage.
#: They lived as private literals on the READING side alone until 2026-09-12,
#: and nothing ever wrote them: prod's 13 webhook trigger events carried
#: neither key, so the binding branch (and the ``trigger.filters`` it gates)
#: had never once run. One definition, so a rename cannot land on one end.
#:
#: ``resource_id`` is always the STRINGIFIED connector-side id — telegram sends
#: ``chat_id`` as a JSON number while the binding row stores ``"8242700007"``.
PAYLOAD_KEY_CONNECTOR_ACCOUNT_ID = "connector_account_id"
PAYLOAD_KEY_RESOURCE_ID = "resource_id"

__all__ = [
    "PAYLOAD_KEY_CONNECTOR_ACCOUNT_ID",
    "PAYLOAD_KEY_RESOURCE_ID",
    "SCHEDULE_KIND_PRODUCT_TICK",
]
