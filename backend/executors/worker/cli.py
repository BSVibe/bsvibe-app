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
import sys
from collections.abc import Sequence

import httpx
import structlog

from backend.executors.worker.config import get_worker_settings
from backend.executors.worker.credentials import (
    CredentialsNotFound,
    clear_host_credentials,
    clear_worker_token,
    default_credentials_path,
    default_worker_token_path,
    load_host_credentials,
    save_worker_token,
)
from backend.executors.worker.executors import detect_capabilities
from backend.executors.worker.login import LoginError, run_login
from backend.executors.worker.main import _amain, register

logger = structlog.get_logger(__name__)

_DEFAULT_ISSUER = "https://api.bsvibe.dev"


# ---------------------------------------------------------------------------
# ``bsvibe`` — auth surface
# ---------------------------------------------------------------------------
def _cmd_login(args: argparse.Namespace) -> int:
    issuer = args.issuer or _DEFAULT_ISSUER
    print(f"Opening browser for sign-in at {issuer} …", file=sys.stderr)
    try:
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
    if not removed_host and not removed_worker:
        print("Nothing to clear.", file=sys.stderr)
        return 0
    if removed_host:
        print(f"Removed {default_credentials_path()}", file=sys.stderr)
    if removed_worker:
        print(f"Removed {default_worker_token_path()}", file=sys.stderr)
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


def build_bsvibe_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bsvibe", description="BSVibe CLI.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="Sign in via PKCE loopback OAuth.")
    p_login.add_argument(
        "--issuer",
        default=None,
        help=f"OAuth issuer base URL (default: {_DEFAULT_ISSUER}).",
    )
    p_login.set_defaults(func=_cmd_login)

    p_logout = sub.add_parser("logout", help="Clear cached credentials.")
    p_logout.set_defaults(func=_cmd_logout)

    p_status = sub.add_parser("status", help="Show sign-in status.")
    p_status.set_defaults(func=_cmd_status)

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

    bearer: str | None = None
    if not args.install_token:
        try:
            creds = load_host_credentials()
            bearer = creds.access_token
        except CredentialsNotFound as exc:
            print(
                f"register failed: {exc}\nHint: run `bsvibe login` first.",
                file=sys.stderr,
            )
            return 1

    async with httpx.AsyncClient(base_url=settings.server_url, timeout=30.0) as client:
        try:
            token = await register(
                client,
                name=args.name,
                capabilities=capabilities,
                labels=labels,
                bearer_token=bearer,
                install_token=args.install_token or "",
            )
        except httpx.HTTPStatusError as exc:
            print(
                f"register failed: HTTP {exc.response.status_code} {exc.response.text}",
                file=sys.stderr,
            )
            return 1

    saved = save_worker_token(token)
    print(f"Registered worker {args.name!r}. Token saved at {saved}", file=sys.stderr)
    return 0


def _cmd_register(args: argparse.Namespace) -> int:
    return asyncio.run(_register_once(args))


def _cmd_run(args: argparse.Namespace) -> int:  # noqa: ARG001
    asyncio.run(_amain())
    return 0


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
    p_reg.add_argument(
        "--install-token",
        default="",
        help="(Deprecated) Use the legacy X-Install-Token path instead of "
        "the host OAuth credential. Lift E5 removes this flag.",
    )
    p_reg.set_defaults(func=_cmd_register)

    p_run = sub.add_parser("run", help="Start the long-polling worker loop.")
    p_run.set_defaults(func=_cmd_run)

    p_logout = sub.add_parser("logout", help="Clear the local worker token.")
    p_logout.set_defaults(func=_cmd_logout)

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
