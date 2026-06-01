"""Workflow infrastructure — delivery persistence + git CLI wrapper.

Per v8 D29 (Delivery absorption into Workflow context), the IO adapters
for the Deliver stage live here:

* :mod:`db` — ``delivery_events`` + ``safe_mode_queue_items`` SQLAlchemy rows
  and the ``SafeModeStatus`` enum.
* :mod:`git_ops` — thin async wrapper over the ``git`` CLI used by the
  github outbound for repo-native delivery (PR mint flow).
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
