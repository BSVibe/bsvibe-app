"""Decision ``kind`` vocabulary shared by the checkpoint and delivery-resolution services.

Lives in ``backend.common`` (a leaf that imports nothing) so both ``checkpoint_resolution`` and
``run_delivery_resolution`` can read it without importing each other, which the import-linter
contracts forbid.
"""

from __future__ import annotations

#: Decision kinds that REPORT on work already out of the agent's hands. A free-text reply only
#: records the founder's words and never resumes the run, and a pending one must never keep a
#: delivered run from shipping: only a real checkpoint (a question the run waits on) does.
#: Kinds answered ON an ended run (redelivery / stalled-PR fix); a terminal run must NOT auto-close them.
KEEP_PENDING_ON_TERMINAL_KINDS: frozenset[str] = frozenset(
    {"delivery_failed", "merge_watch_stalled"}
)

REPORT_DECISION_KINDS: frozenset[str] = frozenset(
    {"merge_watch_stalled", "product_bundle_conflict", "github_app_permission_missing"}
)
