"""The process boundary of the staleness diagnosis — argv and runner behaviour.

``system_probes`` is exercised through an INJECTED runner, so the three real
commands (the launchd listing, ``ps -o lstart=``, ``git log``) never run here.
Each expected argv is asserted against the constant the probes module exports,
so the command lines live in exactly one place and this file cannot drift from
them.

``run_command`` is the one thing that must genuinely start a process to mean
anything, so it is exercised with harmless coreutils only — never the daemon
listing, ``ps`` or ``git``, and never against real system state.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from backend.executors.worker.staleness import ProbeError, Verdict, diagnose
from backend.executors.worker.staleness_probes import (
    GIT_HEAD_FORMAT,
    LIST_ARGV,
    Runner,
    head_argv,
    run_command,
    start_time_argv,
    system_probes,
)

LAUNCHD_LIST_OUT = (
    "PID\tStatus\tLabel\n"
    "1234\t0\tcom.bsvibe.worker\n"
    "-\t0\tcom.bsvibe.worker.spare\n"
    "88\t0\tcom.apple.Spotlight\n"
    "5678\t0\tcom.bsvibe.relay\n"
)


def _fake_run(
    calls: list[tuple[str, ...]],
    *,
    head_date: str = "2026-08-25T12:00:00+00:00",
) -> Runner:
    def run(argv: Sequence[str]) -> str:
        recorded = tuple(argv)
        calls.append(recorded)
        if recorded == LIST_ARGV:
            return LAUNCHD_LIST_OUT
        if recorded[0] == "ps":
            return "Mon Aug 25 09:14:23 2026\n"
        return "c" * 40 + f"\t{head_date}\n"

    return run


# --------------------------------------------------------------------------- #
# the argv constants — the only place a command line is spelled out
# --------------------------------------------------------------------------- #
def test_the_daemon_listing_argv_asks_for_a_list() -> None:
    assert LIST_ARGV[1] == "list"


def test_the_start_time_argv_suppresses_the_header() -> None:
    # The trailing `=` is what makes stdout either one line or empty, which is
    # how "the process vanished" stays distinguishable from "malformed output".
    assert start_time_argv(1234) == ("ps", "-p", "1234", "-o", "lstart=")


def test_the_head_argv_reads_one_commit_from_the_given_repo() -> None:
    assert head_argv("/repo") == ("git", "-C", "/repo", "log", "-1", GIT_HEAD_FORMAT)
    # %cI is the offset-carrying ISO-8601 committer date; %x09 keeps the two
    # fields separable. Losing either turns the comparison silently wrong.
    assert GIT_HEAD_FORMAT == "--format=%H%x09%cI"


# --------------------------------------------------------------------------- #
# system_probes — the real seam, wired to a fake runner
# --------------------------------------------------------------------------- #
def test_system_probes_issue_the_expected_commands() -> None:
    calls: list[tuple[str, ...]] = []
    probes = system_probes(repo="/repo", run=_fake_run(calls))
    report = diagnose(probes)

    assert LIST_ARGV in calls
    assert start_time_argv(1234) in calls
    assert head_argv("/repo") in calls
    assert report.head.sha == "c" * 40
    # The unloaded daemon has no pid, so no start-time call is made for it.
    assert not any(a[0] == "ps" and "-" in a for a in calls)


def test_system_probes_feed_the_comparison_end_to_end() -> None:
    # The whole point: three stdouts in, the incident's verdict out, with no
    # real command run. HEAD is a month after the `ps` start time, so the
    # answer is stale no matter what the machine's UTC offset is.
    calls: list[tuple[str, ...]] = []
    run = _fake_run(calls, head_date="2026-09-25T12:00:00+00:00")
    report = diagnose(system_probes(repo="/repo", run=run))

    by_label = {d.label: d for d in report.daemons}
    assert by_label["com.bsvibe.worker"].verdict is Verdict.STALE
    assert by_label["com.bsvibe.worker.spare"].verdict is Verdict.NOT_RUNNING
    assert [d.label for d in report.stale] == ["com.bsvibe.relay", "com.bsvibe.worker"]


def test_a_probe_failure_surfaces_as_probe_error() -> None:
    def boom(argv: Sequence[str]) -> str:
        raise ProbeError(f"{argv[0]} failed (exit 1)")

    with pytest.raises(ProbeError, match=r"failed \(exit 1\)"):
        diagnose(system_probes(repo="/repo", run=boom))


def test_a_vanished_pid_prints_nothing_and_is_not_a_parse_error() -> None:
    # `ps -p <gone-pid> -o lstart=` exits 1 with empty stdout in practice; when
    # it exits 0 with nothing, that is absence, not malformed output.
    def run(argv: Sequence[str]) -> str:
        recorded = tuple(argv)
        if recorded == LIST_ARGV:
            return "1234\t0\tcom.bsvibe.worker\n"
        if recorded[0] == "ps":
            return "\n"
        return "c" * 40 + "\t2026-08-25T12:00:00+00:00\n"

    (daemon,) = diagnose(system_probes(repo="/repo", run=run)).daemons
    assert daemon.verdict is Verdict.UNKNOWN
    assert "1234" in daemon.detail


# --------------------------------------------------------------------------- #
# run_command — the default runner, on coreutils
# --------------------------------------------------------------------------- #
def test_run_command_returns_stdout() -> None:
    assert run_command(["echo", "hello"]) == "hello\n"


def test_run_command_forces_the_c_locale() -> None:
    # `ps -o lstart=` renders day/month names in the caller's locale, and
    # parse_ps_lstart only reads English — so the runner must pin LC_ALL=C.
    assert run_command(["sh", "-c", 'printf %s "$LC_ALL"']) == "C"


def test_run_command_raises_on_a_nonzero_exit() -> None:
    with pytest.raises(ProbeError, match="exit 3"):
        run_command(["sh", "-c", "echo nope >&2; exit 3"])


def test_run_command_raises_when_the_binary_is_missing() -> None:
    # The daemon-listing binary does not exist off macOS — that must be a
    # ProbeError, not an OSError escaping from the middle of the diagnosis.
    with pytest.raises(ProbeError, match="could not be run"):
        run_command(["definitely-not-a-real-binary-xyz"])
