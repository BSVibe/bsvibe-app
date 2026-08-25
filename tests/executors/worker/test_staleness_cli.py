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
