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

#: Run payload key: every review comment routed to the run, ``{pr_number, body, author,
#: received_at}`` each — what the reviewer actually asked, kept so "fix it again" can hand it
#: back after the ``pr_comments`` queue was consumed. Never popped either.
COMMENT_RECORD_KEY = "pr_comment_record"

_RECORD_LIMIT = 20


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


def record_comment(
    payload: dict[str, Any], *, pr_number: int, body: str, author: str, received_at: datetime
) -> None:
    """Append a review comment to the payload's durable record (the newest ``_RECORD_LIMIT``)."""
    record = [c for c in payload.get(COMMENT_RECORD_KEY) or [] if isinstance(c, dict)]
    record.append(
        {
            "pr_number": pr_number,
            "body": body,
            "author": author,
            "received_at": received_at.isoformat(),
        }
    )
    payload[COMMENT_RECORD_KEY] = record[-_RECORD_LIMIT:]


def unaddressed_comments(
    payload: Mapping[str, Any] | None, delivered_at: Iterable[datetime | None]
) -> list[dict[str, Any]]:
    """The recorded comments no delivery followed, in the shape ``pr_comments`` carries.

    A comment is addressed once a deliverable was delivered at or after it arrived; one with
    no readable arrival time is kept (better asked twice than lost).
    """
    latest = max((_aware(d) for d in delivered_at if d is not None), default=None)
    out: list[dict[str, Any]] = []
    for entry in (payload or {}).get(COMMENT_RECORD_KEY) or []:
        if not isinstance(entry, dict) or not str(entry.get("body") or "").strip():
            continue
        try:
            arrived: datetime | None = _aware(datetime.fromisoformat(str(entry["received_at"])))
        except (KeyError, ValueError):
            arrived = None
        if latest is not None and arrived is not None and latest >= arrived:
            continue
        out.append(
            {
                "pr_number": entry.get("pr_number"),
                "body": entry["body"],
                "author": entry.get("author"),
            }
        )
    return out


__all__ = [
    "COMMENT_RECEIVED_AT_KEY",
    "COMMENT_RECORD_KEY",
    "comment_received_at",
    "record_comment",
    "review_comment_unaddressed",
    "unaddressed_comments",
]
