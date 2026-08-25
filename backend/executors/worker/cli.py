"""``bsvibe`` / ``bsvibe-worker`` CLI dispatcher — Lift E4.

The GitHub-Actions-runner-style worker UX boils down to three commands::

    $ bsvibe login                                  # PKCE loopback OAuth
    $ bsvibe-worker register --name mac-mini --capabilities claude_code
    $ bsvibe-worker run

This module provides the argparse front-end. Sub-commands intentionally use
the simplest possible options surface — the founder's path is a single
``--name`` plus comma-separated ``--capabilities``. Power-user knobs live in
the ``BSVIBE_WORKER_*`` env (already documented in
:mod:`backend.executors.worker.config`).

Entry-point hooks (registered via ``[project.scripts]`` in pyproject.toml):

* ``bsvibe`` → :func:`run_bsvibe_cli` — login / logout / status.
* ``bsvibe-worker`` → :func:`run_bsvibe_worker_cli` — register / run / logout.

Sub-commands are written so individual operations can be tested directly
without spawning a subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

from backend.executors.worker.claude_auth import default_oauth_path as default_claude_oauth_path
from backend.executors.worker.claude_login import ClaudeLoginError, run_claude_login
from backend.executors.worker.config import get_worker_settings
from backend.executors.worker.credentials import (
    CredentialsNotFound,
    HostCredentials,
    WorkerConfig,
    clear_host_credentials,
    clear_worker_config,
    clear_worker_token,
    default_credentials_path,
    default_worker_config_path,
    default_worker_token_path,
    load_host_credentials,
    load_worker_config,
    load_worker_token,
    save_worker_config,
    save_worker_token,
)
from backend.executors.worker.executors import detect_capabilities
from backend.executors.worker.login import (
    LoginError,
    run_login,
    run_login_device,
    run_login_manual,
)
from backend.executors.worker.main import _amain, register
from backend.executors.worker.service import (
    ServiceError,
    build_plan,
    install_service,
    service_status,
    uninstall_service,
)
from backend.executors.worker.staleness import ProbeError, StalenessReport, Verdict, diagnose
from backend.executors.worker.staleness_probes import system_probes

logger = structlog.get_logger(__name__)

_DEFAULT_ISSUER = "https://api.bsvibe.dev"


# ---------------------------------------------------------------------------
# ``bsvibe`` — auth surface
# ---------------------------------------------------------------------------
def _cmd_login(args: argparse.Namespace) -> int:
    issuer = args.issuer or _DEFAULT_ISSUER
    manual = bool(getattr(args, "manual", False))
    device = bool(getattr(args, "device", False))
    if manual and device:
        print("choose one of --manual or --device, not both", file=sys.stderr)
        return 1
    try:
        if device:
            # No loopback listener AND no way to paste a callback back: the
            # human approves a short code elsewhere and this process polls.
            print(f"Device sign-in at {issuer} …", file=sys.stderr)
            result = run_login_device(issuer=issuer)
        elif manual:
            # Remote/headless host: no loopback server. The authorize URL +
            # paste-back instructions are emitted to stderr by the flow itself.
            print(f"Manual (out-of-band) sign-in at {issuer} …", file=sys.stderr)
            result = run_login_manual(issuer=issuer)
        else:
            print(f"Opening browser for sign-in at {issuer} …", file=sys.stderr)
            result = run_login(issuer=issuer)
    except LoginError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Signed in. Credentials saved at {default_credentials_path()}",
        file=sys.stderr,
    )
    _ = result  # keep linters happy — payload is the side-effect (file write)
    return 0


def _cmd_logout(args: argparse.Namespace) -> int:  # noqa: ARG001
    removed_host = clear_host_credentials()
    removed_worker = clear_worker_token()
    removed_config = clear_worker_config()
    if not removed_host and not removed_worker and not removed_config:
        print("Nothing to clear.", file=sys.stderr)
        return 0
    if removed_host:
        print(f"Removed {default_credentials_path()}", file=sys.stderr)
    if removed_worker:
        print(f"Removed {default_worker_token_path()}", file=sys.stderr)
    if removed_config:
        print(f"Removed {default_worker_config_path()}", file=sys.stderr)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    try:
        creds = load_host_credentials()
    except CredentialsNotFound as exc:
        print(f"Not signed in: {exc}", file=sys.stderr)
        return 1
    print(f"Signed in. issuer={creds.issuer or '(unknown)'}", file=sys.stderr)
    if creds.expires_at:
        print(f"access token expires_at={creds.expires_at}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# ``bsvibe pat`` — personal access tokens for browserless clients
# ---------------------------------------------------------------------------
# The loop this closes: ``bsvibe login --manual`` already signs in from a
# remote/headless host (print the authorize URL, paste the redirect back), but a
# session credential is short-lived and is not what an MCP client can hold. A
# PAT is. With these commands the whole path stays on this terminal::
#
#     $ bsvibe login --manual
#     $ TOKEN=$(bsvibe pat create --name mac-mini --quiet)
#     $ claude mcp add --transport http bsvibe https://api.bsvibe.dev/mcp \
#         --header "Authorization: Bearer $TOKEN"
#
# Minting requires the ``mcp:admin`` scope, which ``bsvibe login`` requests and a
# dispatched executor task's token deliberately lacks.

_PAT_PATH = "/api/v1/oauth/pats"


def _pat_transport() -> httpx.BaseTransport | None:
    """Seam so tests can serve these calls without a network. ``None`` = real."""
    return None


def _pat_client(creds: HostCredentials) -> httpx.Client:
    base = (creds.issuer or _DEFAULT_ISSUER).rstrip("/")
    return httpx.Client(
        base_url=base,
        timeout=30.0,
        transport=_pat_transport(),
        headers={"Authorization": f"Bearer {creds.access_token}"},
    )


def _pat_credentials() -> HostCredentials | None:
    try:
        return load_host_credentials()
    except CredentialsNotFound as exc:
        print(
            f"not signed in: {exc}\n"
            "Hint: run `bsvibe login` first — or `bsvibe login --manual` when this "
            "host has no browser (it prints a URL to open anywhere and takes the "
            "redirect pasted back).",
            file=sys.stderr,
        )
        return None


def _pat_http_failed(exc: httpx.HTTPStatusError) -> int:
    """Report the server's own words — a swallowed reason wastes the next hour."""
    print(f"request failed: HTTP {exc.response.status_code} {exc.response.text}", file=sys.stderr)
    return 1


def _cmd_pat_create(args: argparse.Namespace) -> int:
    creds = _pat_credentials()
    if creds is None:
        return 1
    payload: dict[str, object] = {"name": args.name}
    if args.scope:
        payload["scope"] = [s.strip() for s in args.scope.split(",") if s.strip()]
    # Omitted, NOT null — the server's default is "never expires".
    if args.expires_in_days is not None:
        payload["expires_in_days"] = args.expires_in_days

    with _pat_client(creds) as client:
        try:
            resp = client.post(_PAT_PATH, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return _pat_http_failed(exc)
    body = resp.json()

    if args.quiet:
        # Nothing but the token, so `TOKEN=$(… --quiet)` is safe.
        print(body["token"])
        return 0
    print(body["token"])
    print(
        f"\nToken '{body['name']}' created — this is the only time it is shown.\n"
        f"  id      {body['id']}\n"
        f"  scopes  {', '.join(body['scope'])}\n"
        f"  expires {body['expires_at'] or 'never'}\n\n"
        "Add it to an MCP client with:\n"
        f"  claude mcp add --transport http bsvibe {(creds.issuer or _DEFAULT_ISSUER).rstrip('/')}/mcp \\\n"
        '    --header "Authorization: Bearer <token>"',
        file=sys.stderr,
    )
    return 0


def _cmd_pat_list(args: argparse.Namespace) -> int:
    creds = _pat_credentials()
    if creds is None:
        return 1
    with _pat_client(creds) as client:
        try:
            resp = client.get(_PAT_PATH)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return _pat_http_failed(exc)
    rows = resp.json()
    if not rows:
        print("No personal access tokens yet. Create one with `bsvibe pat create --name <name>`.")
        return 0
    for row in rows:
        expires = row["expires_at"] or "never"
        print(f"{row['id']}  {row['name']}  scopes={','.join(row['scope'])}  expires={expires}")
    return 0


def _cmd_pat_revoke(args: argparse.Namespace) -> int:
    creds = _pat_credentials()
    if creds is None:
        return 1
    with _pat_client(creds) as client:
        try:
            resp = client.delete(f"{_PAT_PATH}/{args.pat_id}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return _pat_http_failed(exc)
    print(f"revoked {args.pat_id}", file=sys.stderr)
    return 0


def _add_pat_parser(sub: Any) -> None:
    p_pat = sub.add_parser("pat", help="Manage personal access tokens.")
    pat_sub = p_pat.add_subparsers(dest="pat_cmd", required=True)

    p_create = pat_sub.add_parser("create", help="Mint a token (shown once).")
    p_create.add_argument("--name", required=True, help="Human label, e.g. mac-mini.")
    p_create.add_argument(
        "--scope",
        default=None,
        help="Comma-separated scopes (default: server default, mcp:read).",
    )
    p_create.add_argument(
        "--expires-in-days",
        type=int,
        default=None,
        help="Lifetime in days. Omit for a token that never expires.",
    )
    p_create.add_argument(
        "--quiet",
        action="store_true",
        help="Print ONLY the token, for TOKEN=$(bsvibe pat create … --quiet).",
    )
    p_create.set_defaults(func=_cmd_pat_create)

    p_list = pat_sub.add_parser("list", help="List live tokens (never their values).")
    p_list.set_defaults(func=_cmd_pat_list)

    p_revoke = pat_sub.add_parser("revoke", help="Revoke a token by id.")
    p_revoke.add_argument("pat_id", help="The token id from `bsvibe pat list`.")
    p_revoke.set_defaults(func=_cmd_pat_revoke)


def build_bsvibe_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bsvibe", description="BSVibe CLI.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="Sign in via PKCE loopback OAuth.")
    p_login.add_argument(
        "--issuer",
        default=None,
        help=f"OAuth issuer base URL (default: {_DEFAULT_ISSUER}).",
    )
    p_login.add_argument(
        "--manual",
        action="store_true",
        help=(
            "Out-of-band sign-in for remote/headless hosts: print the authorize "
            "URL and paste the redirect URL (or code) back — no loopback server."
        ),
    )
    p_login.add_argument(
        "--device",
        action="store_true",
        help=(
            "RFC 8628 device sign-in for a host with no browser AND no way to "
            "paste a callback back: shows a short code to enter elsewhere, then "
            "polls until you approve. Nothing is pasted back."
        ),
    )
    p_login.set_defaults(func=_cmd_login)

    p_logout = sub.add_parser("logout", help="Clear cached credentials.")
    p_logout.set_defaults(func=_cmd_logout)

    p_status = sub.add_parser("status", help="Show sign-in status.")
    p_status.set_defaults(func=_cmd_status)

    _add_pat_parser(sub)

    return parser


def run_bsvibe_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_bsvibe_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


# ---------------------------------------------------------------------------
# ``bsvibe-worker`` — register / run / logout
# ---------------------------------------------------------------------------
async def _register_once(args: argparse.Namespace) -> int:
    settings = get_worker_settings()
    capabilities = [c.strip() for c in (args.capabilities or "").split(",") if c.strip()]
    if not capabilities:
        capabilities = detect_capabilities() or ["claude_code"]
    labels = [lab.strip() for lab in (args.labels or "").split(",") if lab.strip()]

    try:
        creds = load_host_credentials()
    except CredentialsNotFound as exc:
        print(
            f"register failed: {exc}\nHint: run `bsvibe login` first.",
            file=sys.stderr,
        )
        return 1
    bearer = creds.access_token

    async with httpx.AsyncClient(base_url=settings.server_url, timeout=30.0) as client:
        try:
            token = await register(
                client,
                name=args.name,
                capabilities=capabilities,
                labels=labels,
                bearer_token=bearer,
            )
        except httpx.HTTPStatusError as exc:
            print(
                f"register failed: HTTP {exc.response.status_code} {exc.response.text}",
                file=sys.stderr,
            )
            return 1

    saved = save_worker_token(token)
    # Lift E12 — persist register-time config to ``~/.bsvibe/config.json`` so
    # subsequent ``bsvibe-worker run`` from any CWD recovers name +
    # capabilities + labels + server_url without re-detecting from hostname /
    # PATH / a CWD-relative ``.env``.
    config = WorkerConfig(
        name=args.name,
        capabilities=capabilities,
        labels=labels,
        server_url=settings.server_url,
        saved_at=int(time.time()),
    )
    config_path = save_worker_config(config)
    print(
        f"Registered worker {args.name!r}. Token saved at {saved}; config saved at {config_path}",
        file=sys.stderr,
    )
    return 0


def _cmd_register(args: argparse.Namespace) -> int:
    return asyncio.run(_register_once(args))


def _cmd_run(args: argparse.Namespace) -> int:  # noqa: ARG001
    asyncio.run(_amain())
    return 0


def _cmd_claude_login(args: argparse.Namespace) -> int:
    """Mint the worker its OWN Claude OAuth token (independent refresh family).

    Runs an authorize-code PKCE flow against the Claude Code OAuth app and
    persists the token pair into ``~/.bsvibe/claude_oauth.json``. Because it is a
    fresh grant (not a copy of the CLI's creds) the worker no longer shares a
    refresh family with the interactive login — eliminating the mutual-burn.
    """
    manual = bool(getattr(args, "manual", False))
    try:
        if manual:
            print("Manual (out-of-band) Claude sign-in — follow the printed URL.", file=sys.stderr)
        else:
            print("Opening browser for Claude sign-in …", file=sys.stderr)
        result = run_claude_login(manual=manual)
    except ClaudeLoginError as exc:
        print(f"claude-login failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Claude token saved to {default_claude_oauth_path()} "
        f"(expires_at={result.expires_at_ms}). Restart the worker to pick it up.",
        file=sys.stderr,
    )
    return 0


def _resolve_worker_exec() -> str:
    """Absolute path to the ``bsvibe-worker`` entry point to run under the service."""
    found = shutil.which("bsvibe-worker")
    if found:
        return found
    return str(Path(sys.executable).parent / "bsvibe-worker")


def _service_runner(cmd: list[str]) -> None:
    print("  $ " + " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, check=True)  # noqa: S603 — fixed launchctl/systemctl argv


def _cmd_service(args: argparse.Namespace) -> int:
    """Install/uninstall/status the worker as an auto-restart OS service.

    GitHub-Actions-runner style: renders the launchd/systemd unit from the saved
    register config so the self-hosted worker survives crashes + reboots. The
    unit carries NO token — ``bsvibe-worker run`` reads ``~/.bsvibe/worker.token``.
    """
    config = load_worker_config()
    if config is None:
        print(
            "no worker config — run `bsvibe-worker register --name <name>` first.",
            file=sys.stderr,
        )
        return 1
    try:
        plan = build_plan(
            config,
            platform=platform.system().lower(),
            home=Path.home(),
            exec_path=_resolve_worker_exec(),
            repo=args.repo or os.getcwd(),
        )
    except ServiceError as exc:
        print(f"service: {exc}", file=sys.stderr)
        return 1

    action = args.service_action
    try:
        if action == "install":
            install_service(plan, run=_service_runner)
            print(
                f"Installed {plan.unit_path} — worker now runs durably with auto-restart.",
                file=sys.stderr,
            )
        elif action == "uninstall":
            uninstall_service(plan, run=_service_runner)
            print(f"Uninstalled {plan.unit_path}.", file=sys.stderr)
        else:  # status
            service_status(plan, run=_service_runner)
    except subprocess.CalledProcessError as exc:
        print(f"service {action} failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_worker_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Print the persisted worker config + token presence — Lift E12.

    The diagnostic founders want every time something looks off. No JSON
    output mode, no flags — dirt simple by design.
    """
    config_path = default_worker_config_path()
    token_path = default_worker_token_path()
    config = load_worker_config(path=config_path)
    token = load_worker_token(path=token_path)

    if config is None and token is None:
        print(
            "No worker config or token found. "
            "Run `bsvibe-worker register --name <name> --capabilities <cap1,cap2>` first.",
            file=sys.stderr,
        )
        return 1

    if config is not None:
        when = datetime.fromtimestamp(config.saved_at, tz=UTC).isoformat()
        caps = ", ".join(config.capabilities) if config.capabilities else "(none)"
        labels = ", ".join(config.labels) if config.labels else "(none)"
        print(f"Worker config: {config_path}", file=sys.stderr)
        print(f"  name: {config.name}", file=sys.stderr)
        print(f"  capabilities: {caps}", file=sys.stderr)
        print(f"  labels: {labels}", file=sys.stderr)
        print(f"  server: {config.server_url}", file=sys.stderr)
        print(f"  registered at: {when}", file=sys.stderr)
    else:
        print(
            f"No worker config at {config_path}. Run `bsvibe-worker register` to create one.",
            file=sys.stderr,
        )

    if token is not None:
        print(f"Token: {token_path} (mode 0600, len={len(token)})", file=sys.stderr)
    else:
        print(
            f"No worker token at {token_path}. Run `bsvibe-worker register` to create one.",
            file=sys.stderr,
        )
    return 0


def _format_staleness_report(report: StalenessReport) -> str:
    head_short = report.head.sha[:12]
    lines = [f"HEAD {head_short} committed at {report.head.committed_at.isoformat()}"]
    if not report.daemons:
        lines.append("No com.bsvibe.* daemons registered with launchd.")
        return "\n".join(lines)
    for daemon in report.daemons:
        if daemon.verdict is Verdict.STALE:
            lines.append(f"STALE    {daemon.label} (pid {daemon.pid}) — {daemon.detail}")
        elif daemon.verdict is Verdict.CURRENT:
            lines.append(
                f"CURRENT  {daemon.label} (pid {daemon.pid}) — "
                f"started {daemon.started_at.isoformat() if daemon.started_at else '?'}"
            )
        elif daemon.verdict is Verdict.NOT_RUNNING:
            lines.append(f"IDLE     {daemon.label} — {daemon.detail}")
        else:
            lines.append(f"UNKNOWN  {daemon.label} (pid {daemon.pid}) — {daemon.detail}")
    if report.has_stale:
        lines.append("")
        lines.append(
            f"{len(report.stale)} stale daemon(s) running pre-HEAD code — restart them."
        )
    return "\n".join(lines)


def _cmd_staleness(args: argparse.Namespace) -> int:
    """Name any ``com.bsvibe.*`` daemon whose process started before repo HEAD.

    The measured incident: a daemon can be healthy (fresh heartbeat, online
    status) while running days-old code. Process start time is the one signal
    that can't lie about that, so this compares it against HEAD directly.

    ``probes_factory`` defaults to :func:`system_probes` (the real launchctl /
    ps / git seam). Tests inject their own via ``args.probes_factory`` so the
    real system is never touched — the parser never sets this attribute, so
    production dispatch always gets the real one.
    """
    repo = args.repo or os.getcwd()
    probes_factory = getattr(args, "probes_factory", None) or system_probes
    try:
        report = diagnose(probes_factory(repo=repo))
    except ProbeError as exc:
        print(f"staleness: {exc}", file=sys.stderr)
        return 1
    print(_format_staleness_report(report))
    return 1 if report.has_stale else 0


def build_bsvibe_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bsvibe-worker",
        description="BSVibe worker — registers a host that can run CLI executors.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="Register this host as a worker.")
    p_reg.add_argument("--name", required=True, help="Display name for the worker.")
    p_reg.add_argument(
        "--capabilities",
        default="",
        help="Comma-separated capabilities (default: auto-detect).",
    )
    p_reg.add_argument("--labels", default="", help="Comma-separated labels (free-form tags).")
    p_reg.set_defaults(func=_cmd_register)

    p_run = sub.add_parser("run", help="Start the long-polling worker loop.")
    p_run.set_defaults(func=_cmd_run)

    p_status = sub.add_parser("status", help="Show persisted worker config + token.")
    p_status.set_defaults(func=_cmd_worker_status)

    p_logout = sub.add_parser("logout", help="Clear the local worker token.")
    p_logout.set_defaults(func=_cmd_logout)

    p_claude = sub.add_parser(
        "claude-login",
        help="Mint the worker's OWN Claude OAuth token (independent refresh family).",
    )
    p_claude.add_argument(
        "--manual",
        action="store_true",
        help=(
            "Remote/headless: print the authorize URL and paste back the "
            "`code#state` (or redirect URL) — no loopback server."
        ),
    )
    p_claude.set_defaults(func=_cmd_claude_login)

    p_service = sub.add_parser(
        "service",
        help="Install/uninstall the worker as an auto-restart OS service (launchd/systemd).",
    )
    svc_sub = p_service.add_subparsers(dest="service_action", required=True)
    for _act, _help in (
        ("install", "Render + install the service (durable, auto-restart)."),
        ("uninstall", "Stop + remove the service."),
        ("status", "Show the service status."),
    ):
        _sp = svc_sub.add_parser(_act, help=_help)
        _sp.add_argument(
            "--repo",
            default=None,
            help="BSVibe checkout dir the worker runs from (default: current dir).",
        )
        _sp.set_defaults(func=_cmd_service)

    p_stale = sub.add_parser(
        "staleness",
        help="Diagnose com.bsvibe.* daemons still running pre-HEAD code.",
    )
    p_stale.add_argument(
        "--repo",
        default=None,
        help="BSVibe checkout dir to compare HEAD against (default: current dir).",
    )
    p_stale.set_defaults(func=_cmd_staleness)

    return parser


def run_bsvibe_worker_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_bsvibe_worker_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


__all__ = [
    "build_bsvibe_parser",
    "build_bsvibe_worker_parser",
    "run_bsvibe_cli",
    "run_bsvibe_worker_cli",
]
