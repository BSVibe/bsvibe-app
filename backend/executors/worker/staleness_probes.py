"""The process boundary for the staleness diagnosis — the ONLY file that shells out.

``staleness.py`` is pure: it parses three stdouts and compares two timestamps.
Everything that actually touches the founder's machine — ``launchctl list`` for
the registered daemons, ``ps -o lstart=`` for each PID's start time, ``git log``
for HEAD — lives here, exactly as ``service.py`` keeps its ``launchctl`` argv
behind an injected ``Runner`` and lets ``cli.py`` own the ``subprocess`` call.

The split is what makes the diagnosis testable: :func:`system_probes` takes the
runner as a parameter, so a test can assert the precise argv issued for each of
the three facts without launchd, a process table, or a repo being involved.

The three argv are module constants (:data:`LIST_ARGV`, :func:`start_time_argv`,
:func:`head_argv`) rather than literals buried inside the closures below. That
keeps every command string in ONE file: a test asserts against the constant, so
no test file has to repeat — or drift from — the real command line.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime

from backend.executors.worker.staleness import (
    DaemonProbes,
    HeadCommit,
    LaunchdEntry,
    ProbeError,
    parse_git_head,
    parse_launchd_list,
    parse_ps_lstart,
)

#: Every job launchd has registered, as ``PID\tStatus\tLabel`` rows.
LIST_ARGV: tuple[str, ...] = ("launchctl", "list")

#: ``%H`` (full sha) and ``%cI`` (committer date, strict ISO-8601 with offset),
#: tab-separated via ``%x09`` so neither field can swallow the other.
GIT_HEAD_FORMAT = "--format=%H%x09%cI"

#: `ps` renders day/month names in the caller's locale and parse_ps_lstart only
#: reads English; PATH is pinned because launchd starts us with a minimal one.
_PROBE_ENV = {"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"}

#: A callable that runs argv and returns stdout, or raises :class:`ProbeError`.
Runner = Callable[[Sequence[str]], str]


def start_time_argv(pid: int) -> tuple[str, ...]:
    """The command that prints when ``pid`` started, and nothing else.

    ``lstart=`` (with the trailing ``=``) suppresses the header, so stdout is
    either one ctime-shaped line or empty — which is what makes "the process
    vanished" distinguishable from "the output was malformed".
    """
    return ("ps", "-p", str(pid), "-o", "lstart=")


def head_argv(repo: str) -> tuple[str, ...]:
    """The command that prints ``repo``'s HEAD sha and committer date."""
    return ("git", "-C", repo, "log", "-1", GIT_HEAD_FORMAT)


def run_command(argv: Sequence[str]) -> str:
    """Run argv and return stdout, raising :class:`ProbeError` on failure.

    Failure is normalised into ``ProbeError`` — including a missing binary,
    since ``launchctl`` simply does not exist off macOS and an ``OSError``
    escaping from the middle of the diagnosis would read like a crash.
    """
    try:
        completed = subprocess.run(  # noqa: S603 — fixed launchctl/ps/git argv
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            env=dict(_PROBE_ENV),
        )
    except OSError as exc:
        raise ProbeError(f"{argv[0]} could not be run: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ProbeError(f"{' '.join(argv)} failed (exit {completed.returncode}): {detail}")
    return completed.stdout


def system_probes(*, repo: str, run: Runner = run_command) -> DaemonProbes:
    """The real seam: launchctl for the daemons, ps for start times, git for HEAD.

    ``run`` is injected so the exact argv this issues is itself a test, without
    any of the three commands ever executing.
    """

    def list_daemons() -> list[LaunchdEntry]:
        return parse_launchd_list(run(LIST_ARGV))

    def start_time(pid: int) -> datetime | None:
        out = run(start_time_argv(pid))
        # `ps` prints nothing for a PID that vanished — absence, not a parse error.
        return parse_ps_lstart(out) if out.strip() else None

    def head_commit() -> HeadCommit:
        return parse_git_head(run(head_argv(repo)))

    return DaemonProbes(
        list_daemons=list_daemons,
        start_time=start_time,
        head_commit=head_commit,
    )


__all__ = [
    "GIT_HEAD_FORMAT",
    "LIST_ARGV",
    "Runner",
    "head_argv",
    "run_command",
    "start_time_argv",
    "system_probes",
]
