"""Diagnose ``bsvibe`` launchd daemons that are still running PRE-HEAD code.

The measured incident: ``com.bsvibe.worker`` picked up a live-workspace task
while running 13-day-old code and aborted it. Nothing on any runtime surface
showed it — the heartbeat was fresh and ``status='online'``, because a stale
daemon is a perfectly healthy daemon that happens to be executing yesterday's
module. The ONE honest signal is the process start time: a daemon that started
BEFORE the current HEAD commit cannot possibly be running HEAD's code.

So the diagnosis is a join across three different system facts, each of which
comes from a different command:

* which daemons launchd has registered, and their PIDs — the service listing
* when each of those PIDs actually started — ``ps -p <pid> -o lstart=``
* when HEAD was committed — ``git log -1 --format=%H%x09%cI``

All three are behind :class:`DaemonProbes`, a struct of three callables, so the
comparison logic can be tested without a launchd, a process table, or a repo.
This module holds ONLY that logic and the pure parsers for those three stdouts:
like ``service.py`` next door, it never spawns a process itself. The wiring that
actually runs those three commands lives in ``staleness_probes.py``, which
builds a :class:`DaemonProbes` from an injected runner — that module owns every
argv literal, and this one owns none.

Two failure modes are designed against explicitly:

* **Under-matching.** A diagnosis that misses a daemon is worse than none —
  it reports "all current" while the stale one keeps aborting tasks. The label
  predicate matches the whole ``com.bsvibe.*`` family, not one hardcoded label.
* **Naive datetimes.** ``ps -o lstart=`` prints local wall-clock with no offset;
  git's ``%cI`` carries one. Comparing the two naively is off by the UTC offset,
  which is exactly the size of the "is it a bit stale?" question. Every time
  here is timezone-aware, and a naive one is refused rather than mis-compared.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import Enum

#: Every daemon this tool installs lives under this prefix. The trailing dot
#: matters: without it ``com.bsvibeer.worker`` would match.
DAEMON_LABEL_PREFIX = "com.bsvibe."

#: What launchd prints in the PID column for a registered-but-not-running job.
NOT_RUNNING_PID = "-"

_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"
_LSTART_FIELDS = 5


class ProbeError(Exception):
    """A system probe failed, or returned something we refuse to guess about."""


class Verdict(Enum):
    """What we can say about one registered daemon."""

    #: Started before HEAD was committed — it is running older code.
    STALE = "stale"
    #: Started at or after HEAD — as far as start time can tell, it is current.
    CURRENT = "current"
    #: Registered with launchd but not currently running (no PID in the listing).
    NOT_RUNNING = "not_running"
    #: We could not read a start time; ``detail`` says why.
    UNKNOWN = "unknown"


#: Report ordering. Stale first — the whole point of running this is to see them.
_VERDICT_ORDER = {
    Verdict.STALE: 0,
    Verdict.CURRENT: 1,
    Verdict.UNKNOWN: 2,
    Verdict.NOT_RUNNING: 3,
}


@dataclass(frozen=True)
class LaunchdEntry:
    """One row of launchd's service listing: a registered label, and its PID."""

    label: str
    #: ``None`` when the listing showed ``-`` — registered, no live process.
    pid: int | None = None


@dataclass(frozen=True)
class HeadCommit:
    """The commit the daemons are being judged against."""

    sha: str
    #: Normalised to UTC so it compares cleanly with a start time in any zone.
    committed_at: datetime


@dataclass(frozen=True)
class DaemonStatus:
    """One daemon's verdict, and the evidence behind it."""

    label: str
    pid: int | None
    started_at: datetime | None
    verdict: Verdict
    #: How far the process start time predates HEAD. Only set when STALE.
    behind_by: timedelta | None = None
    #: Why the verdict is UNKNOWN / NOT_RUNNING, in words a human can act on.
    detail: str = ""


@dataclass(frozen=True)
class StalenessReport:
    """The whole diagnosis: what HEAD is, and where every daemon stands."""

    head: HeadCommit
    daemons: tuple[DaemonStatus, ...] = ()

    @property
    def stale(self) -> tuple[DaemonStatus, ...]:
        return tuple(d for d in self.daemons if d.verdict is Verdict.STALE)

    @property
    def has_stale(self) -> bool:
        return any(d.verdict is Verdict.STALE for d in self.daemons)


@dataclass(frozen=True)
class DaemonProbes:
    """The three system facts the diagnosis needs, each injectable.

    Kept as three narrow callables rather than one fat interface so a test can
    replace exactly the fact it is exercising and leave the rest trivial.
    """

    #: Every ``com.bsvibe.*`` daemon launchd knows about.
    list_daemons: Callable[[], list[LaunchdEntry]]
    #: When the given PID started, or ``None`` if the process is gone.
    start_time: Callable[[int], datetime | None]
    #: The repo's current HEAD.
    head_commit: Callable[[], HeadCommit]


def is_bsvibe_daemon_label(label: str) -> bool:
    """True for any daemon in the ``com.bsvibe.*`` family.

    Deliberately a prefix match, not equality with the one label ``service.py``
    installs: the founder runs several (per-repo, per-name), and the failure
    that hurts is missing the stale one.
    """
    return label.startswith(DAEMON_LABEL_PREFIX) and len(label) > len(DAEMON_LABEL_PREFIX)


def parse_launchd_list(text: str) -> list[LaunchdEntry]:
    """Parse launchd's service listing into the bsvibe daemons it mentions.

    The output is ``PID\\tStatus\\tLabel`` with a header row, but alignment is
    whitespace in practice — so split on runs of whitespace, not on tabs.
    """
    entries: list[LaunchdEntry] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        pid_field, label = fields[0], fields[2]
        if not is_bsvibe_daemon_label(label):
            continue
        # ``-`` means registered but not running; anything non-numeric we treat
        # the same way rather than inventing a PID.
        running = pid_field != NOT_RUNNING_PID and pid_field.lstrip("-").isdigit()
        entries.append(LaunchdEntry(label=label, pid=int(pid_field) if running else None))
    return sorted(entries, key=lambda e: e.label)


def parse_ps_lstart(text: str, *, tz: tzinfo | None = None) -> datetime:
    """Parse macOS ``ps -o lstart=`` output (``Mon Aug 25 09:14:23 2026``).

    Two sharp edges, both of which have silently wrong answers if unhandled:

    * the day is space-padded for single digits (``Aug  4``), so normalise
      whitespace before parsing;
    * the result is LOCAL wall-clock with no offset. We attach ``tz`` (the
      machine's local zone by default) so it can never be compared naively
      against git's offset-carrying timestamp.
    """
    fields = text.split()
    if len(fields) != _LSTART_FIELDS:
        raise ProbeError(f"unexpected `ps -o lstart=` output: {text.strip()!r}")
    try:
        naive = datetime.strptime(" ".join(fields), _LSTART_FORMAT)  # noqa: DTZ007
    except ValueError as exc:
        raise ProbeError(
            f"could not parse `ps -o lstart=` output {text.strip()!r} — "
            "a non-English locale renames the day/month, so run `ps` with LC_ALL=C"
        ) from exc
    return naive.replace(tzinfo=tz or datetime.now().astimezone().tzinfo)


def parse_git_head(text: str) -> HeadCommit:
    """Parse ``git log -1 --format=%H%x09%cI`` output into a :class:`HeadCommit`."""
    sha, _, raw_date = text.strip().partition("\t")
    if not sha or not raw_date:
        raise ProbeError(f"unexpected `git log` output: {text.strip()!r}")
    try:
        committed_at = datetime.fromisoformat(raw_date)
    except ValueError as exc:
        raise ProbeError(f"unparseable commit date {raw_date!r}") from exc
    if committed_at.tzinfo is None:
        # %cI always carries an offset; if it didn't, the comparison below would
        # be wrong by the local UTC offset rather than obviously broken.
        raise ProbeError(f"commit date {raw_date!r} carries no UTC offset")
    return HeadCommit(sha=sha, committed_at=committed_at.astimezone(UTC))


def _judge(
    entry: LaunchdEntry,
    *,
    head: HeadCommit,
    tolerance: timedelta,
    start_time: Callable[[int], datetime | None],
) -> DaemonStatus:
    """Turn one registered daemon into a verdict, never raising for probe failure."""
    if entry.pid is None:
        return DaemonStatus(
            label=entry.label,
            pid=None,
            started_at=None,
            verdict=Verdict.NOT_RUNNING,
            detail="registered with launchd but not running",
        )
    try:
        started_at = start_time(entry.pid)
    except ProbeError as exc:
        # One unreadable process must not blind the report to the others.
        return DaemonStatus(
            label=entry.label,
            pid=entry.pid,
            started_at=None,
            verdict=Verdict.UNKNOWN,
            detail=str(exc),
        )
    if started_at is None:
        # The process can exit between the listing and the start-time read.
        return DaemonStatus(
            label=entry.label,
            pid=entry.pid,
            started_at=None,
            verdict=Verdict.UNKNOWN,
            detail=f"no start time for pid {entry.pid} (process gone?)",
        )
    if started_at.tzinfo is None:
        # Not caught: a naive time is a BUG in the probe, and silently treating
        # it as UTC would produce a confident wrong answer.
        raise ProbeError(f"start time for {entry.label} (pid {entry.pid}) must be timezone-aware")
    behind = head.committed_at - started_at
    if behind > tolerance:
        return DaemonStatus(
            label=entry.label,
            pid=entry.pid,
            started_at=started_at,
            verdict=Verdict.STALE,
            behind_by=behind,
            detail=f"started {behind} before HEAD {head.sha[:12]}",
        )
    return DaemonStatus(
        label=entry.label,
        pid=entry.pid,
        started_at=started_at,
        verdict=Verdict.CURRENT,
    )


def _sort_key(status: DaemonStatus) -> tuple[int, float, str]:
    behind = status.behind_by.total_seconds() if status.behind_by else 0.0
    # Stale first, most-behind first within that, then label for determinism.
    return (_VERDICT_ORDER[status.verdict], -behind, status.label)


def diagnose(probes: DaemonProbes, *, tolerance: timedelta | None = None) -> StalenessReport:
    """Name the daemons whose process started before the current HEAD commit.

    ``tolerance`` absorbs the gap between committing and restarting — a daemon
    that started a few seconds before HEAD was written is a clock/ordering
    artefact, not 13 days of drift. It defaults to zero: strictly earlier than
    HEAD is stale, exactly at HEAD is not.
    """
    slack = tolerance or timedelta(0)
    head = probes.head_commit()
    statuses = [
        _judge(entry, head=head, tolerance=slack, start_time=probes.start_time)
        for entry in probes.list_daemons()
    ]
    return StalenessReport(head=head, daemons=tuple(sorted(statuses, key=_sort_key)))


__all__ = [
    "DAEMON_LABEL_PREFIX",
    "DaemonProbes",
    "DaemonStatus",
    "HeadCommit",
    "LaunchdEntry",
    "NOT_RUNNING_PID",
    "ProbeError",
    "StalenessReport",
    "Verdict",
    "diagnose",
    "is_bsvibe_daemon_label",
    "parse_git_head",
    "parse_launchd_list",
    "parse_ps_lstart",
]
