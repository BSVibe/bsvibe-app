"""CLI-level tests for ``bsvibe-worker staleness`` — kept apart from ``test_cli.py``.

This is the ONE surface where the founder actually meets the diagnosis:
``bsvibe-worker staleness``. Every test here drives it through the real
production code path in :func:`backend.executors.worker.cli._cmd_staleness`,
injecting a ``DaemonProbes`` factory via ``args.probes_factory`` — the seam
``_cmd_staleness`` exposes precisely so this file never has to substitute
anything inside the ``cli`` module itself. Nothing here shells out to
launchctl, ps, or git; that boundary is exercised in ``test_staleness_probes.py``.

Kept in its own file (rather than folded into ``test_cli.py``) so a purity
check can be scoped to exactly the tests this task added, without dragging in
the dozens of pre-existing, unrelated login/register/service tests next door.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from backend.executors.worker import cli as cli_mod
from backend.executors.worker.staleness import (
    DaemonProbes,
    HeadCommit,
    LaunchdEntry,
    ProbeError,
)

HEAD = HeadCommit(sha="a" * 40, committed_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC))

ProbesFactory = Callable[..., DaemonProbes]


def _namespace(*, repo: str | None = None, probes_factory: ProbesFactory) -> argparse.Namespace:
    return argparse.Namespace(repo=repo, probes_factory=probes_factory)


def test_staleness_subcommand_is_registered() -> None:
    parser = cli_mod.build_bsvibe_worker_parser()
    assert "staleness" in parser.format_help()


def test_staleness_repo_option_is_parsed_and_dispatches() -> None:
    parser = cli_mod.build_bsvibe_worker_parser()
    args = parser.parse_args(["staleness", "--repo", "/some/repo"])
    assert args.repo == "/some/repo"
    assert args.func is cli_mod._cmd_staleness


def test_staleness_reports_ok_when_nothing_is_stale(
    capsys: pytest.CaptureFixture[str],
) -> None:
    probes = DaemonProbes(
        list_daemons=lambda: [LaunchdEntry("com.bsvibe.worker", 1234)],
        start_time=lambda pid: HEAD.committed_at + timedelta(minutes=5),  # noqa: ARG005
        head_commit=lambda: HEAD,
    )
    rc = cli_mod._cmd_staleness(_namespace(probes_factory=lambda *, repo: probes))  # noqa: ARG005
    assert rc == 0
    out = capsys.readouterr().out
    assert "CURRENT" in out and "com.bsvibe.worker" in out


def test_staleness_exits_nonzero_and_names_the_stale_daemon(
    capsys: pytest.CaptureFixture[str],
) -> None:
    probes = DaemonProbes(
        list_daemons=lambda: [LaunchdEntry("com.bsvibe.worker", 1234)],
        start_time=lambda pid: HEAD.committed_at - timedelta(days=13),  # noqa: ARG005
        head_commit=lambda: HEAD,
    )
    rc = cli_mod._cmd_staleness(_namespace(probes_factory=lambda *, repo: probes))  # noqa: ARG005
    assert rc == 1
    out = capsys.readouterr().out
    assert "STALE" in out and "com.bsvibe.worker" in out


def test_staleness_reports_a_probe_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def raising_factory(*, repo: str) -> DaemonProbes:  # noqa: ARG001
        raise ProbeError("launchctl failed (exit 1)")

    rc = cli_mod._cmd_staleness(_namespace(probes_factory=raising_factory))
    assert rc == 1
    assert "launchctl failed" in capsys.readouterr().err


def test_staleness_passes_the_resolved_repo_to_the_factory() -> None:
    seen: dict[str, str] = {}

    def recording_factory(*, repo: str) -> DaemonProbes:
        seen["repo"] = repo
        return DaemonProbes(
            list_daemons=list,
            start_time=lambda pid: None,  # noqa: ARG005
            head_commit=lambda: HEAD,
        )

    cli_mod._cmd_staleness(_namespace(repo="/explicit/repo", probes_factory=recording_factory))
    assert seen["repo"] == "/explicit/repo"


def test_staleness_dispatch_defaults_to_the_real_system_probes() -> None:
    # The parser never sets `probes_factory` — production dispatch must fall
    # back to the real seam, not silently no-op.
    parser = cli_mod.build_bsvibe_worker_parser()
    args = parser.parse_args(["staleness"])
    assert not hasattr(args, "probes_factory")


# --------------------------------------------------------------------------
# The diagnosis has to name its own remedy
# --------------------------------------------------------------------------
#
# 2026-08-26, first real use of this command: it printed "restart them." and
# nothing else. The operator (an agent, at session start) then has to source
# the command from somewhere outside the tool — and the OBVIOUS guess, `kill`,
# is the documented WRONG answer: a worker's identity is its token, so killing
# it loses the live registration. A tool that names a fault it will not tell
# you how to fix hands its reader a coin flip between the right command and a
# destructive one.


def _stale_and_current_probes() -> DaemonProbes:
    entries = [
        LaunchdEntry("com.bsvibe.worker", 1234),
        LaunchdEntry("com.bsvibe.worker-admin", 5678),
        LaunchdEntry("com.bsvibe.worker-mac-mini-e2e", 9012),
    ]
    fresh = {9012}

    def start_time(pid: int) -> datetime:
        return (
            HEAD.committed_at + timedelta(minutes=5)
            if pid in fresh
            else (HEAD.committed_at - timedelta(hours=3))
        )

    return DaemonProbes(
        list_daemons=lambda: entries, start_time=start_time, head_commit=lambda: HEAD
    )


def test_staleness_prints_the_exact_restart_command_for_each_stale_daemon(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli_mod._cmd_staleness(
        _namespace(probes_factory=lambda *, repo: _stale_and_current_probes())  # noqa: ARG005
    )
    assert rc == 1
    out = capsys.readouterr().out
    uid = os.getuid()
    assert f"launchctl kickstart -k gui/{uid}/com.bsvibe.worker\n" in out
    assert f"launchctl kickstart -k gui/{uid}/com.bsvibe.worker-admin" in out


def test_the_restart_lines_cover_the_stale_daemons_and_only_those(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A remedy offered for a CURRENT daemon is a pointless restart, and a
    stale one left out is the fault this command exists to surface."""
    cli_mod._cmd_staleness(
        _namespace(probes_factory=lambda *, repo: _stale_and_current_probes())  # noqa: ARG005
    )
    out = capsys.readouterr().out
    restarts = [ln.strip() for ln in out.splitlines() if "kickstart" in ln]
    assert len(restarts) == 2
    assert not any("mac-mini-e2e" in ln for ln in restarts)


def test_staleness_names_kill_as_the_wrong_way(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cheap guess is destructive, so the output has to rule it out where
    the reader is already looking."""
    cli_mod._cmd_staleness(
        _namespace(probes_factory=lambda *, repo: _stale_and_current_probes())  # noqa: ARG005
    )
    assert "kill" in capsys.readouterr().out


def test_a_clean_report_offers_no_remedy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """POSITIVE CONTROL — nothing stale, nothing to run. This assertion passed
    before the remedy existed and must keep passing after."""
    probes = DaemonProbes(
        list_daemons=lambda: [LaunchdEntry("com.bsvibe.worker", 1234)],
        start_time=lambda pid: HEAD.committed_at + timedelta(minutes=5),  # noqa: ARG005
        head_commit=lambda: HEAD,
    )
    rc = cli_mod._cmd_staleness(_namespace(probes_factory=lambda *, repo: probes))  # noqa: ARG005
    assert rc == 0
    assert "kickstart" not in capsys.readouterr().out


def test_the_report_renderer_takes_the_uid_rather_than_reading_the_process(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The gui domain belongs to the user whose launchd owns the daemons, and
    the renderer is pure: it is TOLD the uid, it does not go find one. Same
    discipline as the process/git probes this command already injects."""
    from backend.executors.worker.staleness import diagnose

    report = diagnose(_stale_and_current_probes())
    rendered = cli_mod._format_staleness_report(report, uid=4242)
    assert "gui/4242/com.bsvibe.worker" in rendered
    capsys.readouterr()
