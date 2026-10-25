"""Was a review comment on a run's PR ever answered by a fix that reached the PR?

The run payload's consumed comment queue cannot say: the drive loop consumes (clears) it when it
injects the comments into the turn, so once the run has ``failed`` the queue is gone.
What stays is a pair of facts:

* the comment reached the run — :data:`COMMENT_RECEIVED_AT_KEY`, written by intake and
  never consumed; it holds the time of the LATEST comment;
* a fix reached the PR — a deliverable of the run whose ``delivered_at`` is later.

A comment is unaddressed when the first holds and no delivery came after it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

#: Run payload key: ISO time of the latest review comment routed to the run. Unlike
#: ``pr_comments`` it is a record, not a queue — nothing ever pops it.
COMMENT_RECEIVED_AT_KEY = "pr_comment_received_at"


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def comment_received_at(payload: Mapping[str, Any] | None) -> datetime | None:
    """When the latest review comment reached the run, or ``None`` if none ever did."""
    raw = (payload or {}).get(COMMENT_RECEIVED_AT_KEY)
    if not isinstance(raw, str):
        return None
    try:
        return _aware(datetime.fromisoformat(raw))
    except ValueError:
        return None


def review_comment_unaddressed(
    payload: Mapping[str, Any] | None, delivered_at: Iterable[datetime | None]
) -> bool:
    """A review comment reached the run and no deliverable was delivered after it.

    ``delivered_at`` is the ``delivered_at`` of every deliverable of the run (``None`` =
    not delivered).
    """
    received = comment_received_at(payload)
    if received is None:
        return False
    return not any(d is not None and _aware(d) >= received for d in delivered_at)


__all__ = ["COMMENT_RECEIVED_AT_KEY", "comment_received_at", "review_comment_unaddressed"]
