"""``bsvibe`` launchd daemons running PRE-HEAD code — the staleness diagnosis.

The measured incident: ``com.bsvibe.worker`` picked up a live-workspace task
while running 13-day-old code and aborted it. The heartbeat was fresh and
``status='online'``, so no surface showed the staleness — the ONE honest signal
is the process start time.

Every test here injects fakes through :class:`DaemonProbes`; nothing in this
file runs a real command or reads real system state. The runner that does spawn
processes lives in ``staleness_probes.py`` and is covered next door in
``test_staleness_probes.py``.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from backend.executors.worker import staleness
from backend.executors.worker.service import SERVICE_LABEL
from backend.executors.worker.staleness import (
    DAEMON_LABEL_PREFIX,
    DaemonProbes,
    HeadCommit,
    LaunchdEntry,
    ProbeError,
    StalenessReport,
    Verdict,
    diagnose,
    is_bsvibe_daemon_label,
    parse_git_head,
    parse_launchd_list,
    parse_ps_lstart,
)

HEAD = HeadCommit(sha="a" * 40, committed_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC))


def _at(**delta: float) -> datetime:
    """A start time relative to HEAD's commit time."""
    return HEAD.committed_at + timedelta(**delta)


def _probes(
    entries: list[LaunchdEntry],
    start_times: dict[int, datetime] | None = None,
    *,
    head: HeadCommit = HEAD,
) -> DaemonProbes:
    times = start_times or {}

    def start_time(pid: int) -> datetime | None:
        return times.get(pid)

    return DaemonProbes(
        list_daemons=lambda: entries,
        start_time=start_time,
        head_commit=lambda: head,
    )


# --------------------------------------------------------------------------- #
# label predicate — the under-matching failure mode is the bug we're fixing
# --------------------------------------------------------------------------- #
def test_the_installed_service_label_is_matched() -> None:
    # service.py installs exactly this label; the diagnosis must see it.
    assert is_bsvibe_daemon_label(SERVICE_LABEL)
    assert SERVICE_LABEL.startswith(DAEMON_LABEL_PREFIX)


def test_multiple_bsvibe_daemons_are_all_matched() -> None:
    # The founder runs several — per-repo / per-name labels must not be missed.
    assert is_bsvibe_daemon_label("com.bsvibe.worker.live")
    assert is_bsvibe_daemon_label("com.bsvibe.relay")


def test_foreign_labels_are_not_matched() -> None:
    assert not is_bsvibe_daemon_label("com.apple.Spotlight")
    assert not is_bsvibe_daemon_label("com.bsvibeer.worker")  # prefix must end at the dot
    assert not is_bsvibe_daemon_label("Label")  # the listing's header row


# --------------------------------------------------------------------------- #
# parse_launchd_list — pure, over captured stdout
# --------------------------------------------------------------------------- #
LAUNCHD_LIST_OUT = (
    "PID\tStatus\tLabel\n"
    "1234\t0\tcom.bsvibe.worker\n"
    "-\t0\tcom.bsvibe.worker.spare\n"
    "88\t0\tcom.apple.Spotlight\n"
    "5678\t0\tcom.bsvibe.relay\n"
)


def test_parse_launchd_list_keeps_only_bsvibe_daemons() -> None:
    entries = parse_launchd_list(LAUNCHD_LIST_OUT)
    assert [e.label for e in entries] == [
        "com.bsvibe.relay",
        "com.bsvibe.worker",
        "com.bsvibe.worker.spare",
    ]


def test_parse_launchd_list_reads_pids_and_the_unloaded_dash() -> None:
    by_label = {e.label: e for e in parse_launchd_list(LAUNCHD_LIST_OUT)}
    assert by_label["com.bsvibe.worker"].pid == 1234
    assert by_label["com.bsvibe.relay"].pid == 5678
    # `-` means registered but not currently running — no process, no start time.
    assert by_label["com.bsvibe.worker.spare"].pid is None


def test_parse_launchd_list_tolerates_space_alignment_and_blank_lines() -> None:
    entries = parse_launchd_list("PID  Status  Label\n\n  4321   0   com.bsvibe.worker  \n")
    assert entries == [LaunchdEntry(label="com.bsvibe.worker", pid=4321)]


def test_parse_launchd_list_skips_short_lines() -> None:
    assert parse_launchd_list("garbage\n1 2\n") == []


# --------------------------------------------------------------------------- #
# parse_ps_lstart — macOS `ps -o lstart=`, locale- and tz-sensitive
# --------------------------------------------------------------------------- #
def test_parse_ps_lstart_reads_the_ctime_shape() -> None:
    got = parse_ps_lstart("Mon Aug 25 09:14:23 2026\n", tz=UTC)
    assert got == datetime(2026, 8, 25, 9, 14, 23, tzinfo=UTC)


def test_parse_ps_lstart_handles_the_padded_single_digit_day() -> None:
    got = parse_ps_lstart("Tue Aug  4 07:05:00 2026", tz=UTC)
    assert got == datetime(2026, 8, 4, 7, 5, 0, tzinfo=UTC)


def test_parse_ps_lstart_is_timezone_aware_without_an_explicit_tz() -> None:
    # `lstart` is local wall-clock with no offset; a naive result would blow up
    # (or silently mis-compare) against git's offset-carrying %cI.
    assert parse_ps_lstart("Mon Aug 25 09:14:23 2026").tzinfo is not None


def test_parse_ps_lstart_rejects_a_non_english_month() -> None:
    # If this fires in production, `ps` ran without LC_ALL=C.
    with pytest.raises(ProbeError, match="LC_ALL=C"):
        parse_ps_lstart("lun. août 25 09:14:23 2026")


@pytest.mark.parametrize("text", ["", "Mon Aug 25 2026", "Mon Aug 25 09:14 2026 extra"])
def test_parse_ps_lstart_rejects_malformed_output(text: str) -> None:
    with pytest.raises(ProbeError):
        parse_ps_lstart(text)


# --------------------------------------------------------------------------- #
# parse_git_head
# --------------------------------------------------------------------------- #
def test_parse_git_head_reads_sha_and_committer_date() -> None:
    head = parse_git_head("b" * 40 + "\t2026-08-25T21:00:00+09:00\n")
    assert head.sha == "b" * 40
    assert head.committed_at == datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("text", ["", "deadbeef", "deadbeef\tnot-a-date"])
def test_parse_git_head_rejects_malformed_output(text: str) -> None:
    with pytest.raises(ProbeError):
        parse_git_head(text)


def test_parse_git_head_rejects_an_offsetless_date() -> None:
    with pytest.raises(ProbeError, match="offset"):
        parse_git_head("deadbeef\t2026-08-25T12:00:00")


# --------------------------------------------------------------------------- #
# diagnose — the comparison that names the stale daemon
# --------------------------------------------------------------------------- #
def test_a_daemon_started_before_head_is_stale() -> None:
    # The measured incident: 13-day-old code, fresh heartbeat, status='online'.
    report = diagnose(
        _probes([LaunchdEntry("com.bsvibe.worker", 1234)], {1234: _at(days=-13)}),
    )
    (daemon,) = report.daemons
    assert daemon.verdict is Verdict.STALE
    assert daemon.behind_by == timedelta(days=13)
    assert report.has_stale
    assert [d.label for d in report.stale] == ["com.bsvibe.worker"]


def test_a_daemon_started_after_head_is_current() -> None:
    report = diagnose(_probes([LaunchdEntry("com.bsvibe.worker", 1234)], {1234: _at(minutes=5)}))
    (daemon,) = report.daemons
    assert daemon.verdict is Verdict.CURRENT
    assert daemon.behind_by is None
    assert not report.has_stale


def test_a_daemon_started_exactly_at_head_is_current() -> None:
    # Boundary: equal is NOT earlier, so it is not stale.
    report = diagnose(_probes([LaunchdEntry("com.bsvibe.worker", 1234)], {1234: _at()}))
    assert report.daemons[0].verdict is Verdict.CURRENT


def test_tolerance_absorbs_a_small_lag() -> None:
    probes = _probes([LaunchdEntry("com.bsvibe.worker", 1234)], {1234: _at(seconds=-30)})
    assert diagnose(probes).daemons[0].verdict is Verdict.STALE
    lenient = diagnose(probes, tolerance=timedelta(minutes=1))
    assert lenient.daemons[0].verdict is Verdict.CURRENT


def test_an_unloaded_daemon_has_no_process_to_judge() -> None:
    report = diagnose(_probes([LaunchdEntry("com.bsvibe.worker.spare", None)]))
    (daemon,) = report.daemons
    assert daemon.verdict is Verdict.NOT_RUNNING
    assert daemon.started_at is None
    assert not report.has_stale


def test_a_vanished_pid_is_unknown_not_a_crash() -> None:
    # The process can exit between the daemon listing and the start-time read.
    report = diagnose(_probes([LaunchdEntry("com.bsvibe.worker", 1234)], {}))
    (daemon,) = report.daemons
    assert daemon.verdict is Verdict.UNKNOWN
    assert "1234" in daemon.detail


def test_a_probe_error_is_reported_not_raised() -> None:
    def boom(pid: int) -> datetime:
        raise ProbeError(f"reading pid {pid} failed (exit 1)")

    probes = DaemonProbes(
        list_daemons=lambda: [LaunchdEntry("com.bsvibe.worker", 1234)],
        start_time=boom,
        head_commit=lambda: HEAD,
    )
    (daemon,) = diagnose(probes).daemons
    assert daemon.verdict is Verdict.UNKNOWN
    assert "exit 1" in daemon.detail


def test_stale_daemons_are_reported_first() -> None:
    report = diagnose(
        _probes(
            [
                LaunchdEntry("com.bsvibe.relay", 3),
                LaunchdEntry("com.bsvibe.worker", 1),
                LaunchdEntry("com.bsvibe.worker.spare", None),
                LaunchdEntry("com.bsvibe.zzz", 2),
            ],
            {1: _at(days=-13), 2: _at(days=-1), 3: _at(hours=1)},
        ),
    )
    assert [d.label for d in report.daemons] == [
        "com.bsvibe.worker",  # stale, 13 days behind
        "com.bsvibe.zzz",  # stale, 1 day behind
        "com.bsvibe.relay",  # current
        "com.bsvibe.worker.spare",  # not running
    ]
    assert [d.label for d in report.stale] == ["com.bsvibe.worker", "com.bsvibe.zzz"]


def test_the_report_carries_the_head_it_judged_against() -> None:
    report = diagnose(_probes([]))
    assert isinstance(report, StalenessReport)
    assert report.head == HEAD
    assert report.daemons == ()
    assert not report.has_stale


def test_a_naive_start_time_is_refused_rather_than_mis_compared() -> None:
    naive = datetime(2026, 8, 12, 12, 0)  # noqa: DTZ001 — the bug being guarded
    with pytest.raises(ProbeError, match="timezone-aware"):
        diagnose(_probes([LaunchdEntry("com.bsvibe.worker", 1234)], {1234: naive}))


# --------------------------------------------------------------------------- #
# the seam itself — the pure module must stay pure
# --------------------------------------------------------------------------- #
def _top_level_imports(module: ModuleType) -> set[str]:
    """The distinct top-level packages ``module`` imports, read from its source."""
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_the_pure_module_imports_nothing_that_can_spawn_a_process() -> None:
    # The seam only pays off if the comparison logic stays free of the process
    # boundary: `staleness_probes.py` owns it, like cli.py does for service.py.
    # Asserted as an import allowlist rather than a string search, so it stays
    # true no matter how a future spawn would be spelled — anything that can
    # start a process has to come in through one of these names.
    assert _top_level_imports(staleness) == {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
    }
