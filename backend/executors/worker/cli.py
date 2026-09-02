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
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
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


# ---------------------------------------------------------------------------
# ``bsvibe status`` — 세션 판정
#
# 실측(2026-09-02): status 는 ``expires_at`` 을 **출력만** 하고 지금 시각과
# 비교하지 않았다. 17시간 전에 죽은 토큰에 "Signed in" + exit 0 이 나왔고, 바로
# 다음 명령이 HTTP 401(JWKS resolution failed)로 죽었다. 판정은 반드시 시각
# 비교에서 나온다 — 문구만 바꾸면 같은 거짓말을 더 예쁘게 할 뿐이다.
#
# 상태는 네 가지이고 exit code 와 1:1 이다. 스크립트가 ``bsvibe status`` 로
# 게이트를 걸 수 있어야 하므로 "죽은 세션"이 0 이어서는 안 된다.
class SessionState(IntEnum):
    """``bsvibe status`` 의 판정. **값이 곧 exit code** 다 (단일 출처)."""

    #: 아직 유효하다. ``expires_at`` 이 ``None`` 이라 만료를 **증명할 수 없는**
    #: 경우도 여기다 — 모르는 것과 죽은 것은 다르므로 만료로 단정하지 않고,
    #: 대신 모른다고 말한다.
    SIGNED_IN = 0
    #: 자격증명 자체가 없다. 기존 동작.
    NOT_SIGNED_IN = 1
    #: 만료 + ``refresh_token`` 없음 → 사람이 브라우저 앞에 앉는 수밖에 없다.
    EXPIRED_REAUTH = 2
    #: 만료 + ``refresh_token`` 있음 → 무인 갱신을 붙일 여지가 있다.
    #:
    #: ⚠️ 2 와 3 을 가르는 이유는 자동화다. 다만 오늘 이 저장소에는 **호스트
    #: 자격증명의 refresh_token 을 실제로 교환하는 명령이 없다**. 그래서 3 의
    #: 안내도 지금은 ``bsvibe login`` 을 가리킨다 — 없는 자동 갱신을 있다고
    #: 말하지 않는다. 갱신 명령이 생기면 이 상태의 안내 문구만 바뀐다.
    EXPIRED_REFRESHABLE = 3


@dataclass(frozen=True)
class SessionStatus:
    """판정 + 사람이 읽을 줄들. 렌더링과 분리해 두어야 시각 비교를 테스트한다."""

    state: SessionState
    lines: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return int(self.state)


def _humanize(seconds: float) -> str:
    """``5400`` → ``"1h 30m"``. 초 단위 숫자는 사람이 못 읽는다."""
    total = int(abs(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


def evaluate_session(
    creds: HostCredentials | None,
    *,
    now: float,
    detail: str | None = None,
) -> SessionStatus:
    """``creds`` 를 ``now`` 와 대조해 상태를 판정한다 — 순수 함수.

    ``creds is None`` 은 "자격증명 없음"이고, ``detail`` 은 그 이유(어느 경로를
    봤는지)를 그대로 실어 주기 위한 것이다.
    """
    if creds is None:
        lines = ["Not signed in." if not detail else f"Not signed in: {detail}"]
        lines.append("Run `bsvibe login` to sign in.")
        return SessionStatus(SessionState.NOT_SIGNED_IN, tuple(lines))

    issuer = creds.issuer or "(unknown)"

    if creds.expires_at is None:
        # 모른다 ≠ 죽었다. 초록불을 주되, 모른다는 사실을 숨기지 않는다.
        return SessionStatus(
            SessionState.SIGNED_IN,
            (
                f"Signed in. issuer={issuer}",
                "access token expiry unknown (credentials carry no expires_at) — "
                "cannot tell how long it is still good for.",
            ),
        )

    remaining = creds.expires_at - now
    if remaining > 0:
        return SessionStatus(
            SessionState.SIGNED_IN,
            (
                f"Signed in. issuer={issuer}",
                f"access token valid for {_humanize(remaining)} (expires_at={creds.expires_at})",
            ),
        )

    head = (
        f"Session EXPIRED {_humanize(remaining)} ago "
        f"(expires_at={creds.expires_at}). issuer={issuer}"
    )
    if creds.refresh_token:
        return SessionStatus(
            SessionState.EXPIRED_REFRESHABLE,
            (
                head,
                "A refresh token is stored, so the session can be renewed at the "
                "issuer — but no bsvibe command redeems it yet.",
                "Run `bsvibe login` again (add --manual or --device on a host with no browser).",
            ),
        )
    return SessionStatus(
        SessionState.EXPIRED_REAUTH,
        (
            head,
            "No refresh token is stored — this session cannot be renewed.",
            "Run `bsvibe login` again (add --manual or --device on a host with no browser).",
        ),
    )


def _cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    creds: HostCredentials | None
    detail: str | None
    try:
        creds, detail = load_host_credentials(), None
    except CredentialsNotFound as exc:
        creds, detail = None, str(exc)
    status = evaluate_session(creds, now=time.time(), detail=detail)
    for line in status.lines:
        print(line, file=sys.stderr)
    return status.exit_code


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


def _api_transport() -> httpx.BaseTransport | None:
    """Seam so tests can serve these calls without a network. ``None`` = real.

    PAT 명령만의 것이 아니다 — 이슈어(=백엔드, ``api.bsvibe.dev``)에 인증해 붙는
    모든 CLI 조회가 같은 관문을 쓴다. 제품 표면을 붙이며 두 번째 클라이언트를
    만들 뻔했고, 그러면 인증 헤더·타임아웃·오류 보고가 두 벌로 갈라진다.
    """
    return None


def _api_client(creds: HostCredentials) -> httpx.Client:
    base = (creds.issuer or _DEFAULT_ISSUER).rstrip("/")
    return httpx.Client(
        base_url=base,
        timeout=30.0,
        transport=_api_transport(),
        headers={"Authorization": f"Bearer {creds.access_token}"},
    )


def _api_credentials() -> HostCredentials | None:
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


def _api_http_failed(exc: httpx.HTTPStatusError) -> int:
    """Report the server's own words — a swallowed reason wastes the next hour."""
    print(f"request failed: HTTP {exc.response.status_code} {exc.response.text}", file=sys.stderr)
    return 1


def _cmd_pat_create(args: argparse.Namespace) -> int:
    creds = _api_credentials()
    if creds is None:
        return 1
    payload: dict[str, object] = {"name": args.name}
    if args.scope:
        payload["scope"] = [s.strip() for s in args.scope.split(",") if s.strip()]
    # Omitted, NOT null — the server's default is "never expires".
    if args.expires_in_days is not None:
        payload["expires_in_days"] = args.expires_in_days

    with _api_client(creds) as client:
        try:
            resp = client.post(_PAT_PATH, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return _api_http_failed(exc)
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
    creds = _api_credentials()
    if creds is None:
        return 1
    with _api_client(creds) as client:
        try:
            resp = client.get(_PAT_PATH)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return _api_http_failed(exc)
    rows = resp.json()
    if not rows:
        print("No personal access tokens yet. Create one with `bsvibe pat create --name <name>`.")
        return 0
    for row in rows:
        expires = row["expires_at"] or "never"
        print(f"{row['id']}  {row['name']}  scopes={','.join(row['scope'])}  expires={expires}")
    return 0


def _cmd_pat_revoke(args: argparse.Namespace) -> int:
    creds = _api_credentials()
    if creds is None:
        return 1
    with _api_client(creds) as client:
        try:
            resp = client.delete(f"{_PAT_PATH}/{args.pat_id}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return _api_http_failed(exc)
    print(f"revoked {args.pat_id}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# ``bsvibe products`` / ``runs`` / ``deliverables`` — 터미널에서 제품을 본다.
#
# REST 는 이미 다 있었다. 없던 것은 서브시스템이 아니라 명령 몇 개다: CLI 는
# auth + 워커 등록 전용이었고(실측 2026-08-31), SSH 로 붙은 호스트에서 "지금 뭐가
# 돌고 있지" 를 물으려면 브라우저를 열거나 MCP 클라이언트를 붙여야 했다.
#
# ⚠️ 조회만 연다. 터미널에서 런을 취소하거나 산출물을 회수하는 표면은 승인
# 흐름(Safe Mode·체크포인트)을 우회하므로 별도 결정이다.
# ---------------------------------------------------------------------------
_PRODUCTS_PATH = "/api/v1/products"
_RUNS_PATH = "/api/v1/runs"
_DELIVERABLES_PATH = "/api/v1/deliverables"

#: 목록 한 줄에 싣는 지시문 길이. 지시문은 여러 줄이라 그대로 두면 목록이
#: 깨져 한 화면에 안 들어온다 — 자른다는 사실은 말줄임표로 보인다.
_INTENT_CELL = 60


def _one_line(text: str, *, width: int = _INTENT_CELL) -> str:
    """여러 줄 텍스트를 목록 한 칸으로 — 자를 때는 잘랐다고 보인다."""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _fetch(path: str, params: dict[str, Any] | None = None) -> Any | None:
    """인증된 GET 하나. 실패는 서버의 말 그대로 보고하고 ``None``."""
    creds = _api_credentials()
    if creds is None:
        return None
    with _api_client(creds) as client:
        try:
            resp = client.get(path, params=params or None)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _api_http_failed(exc)
            return None
    return resp.json()


def _print_ids(rows: list[dict[str, Any]]) -> int:
    """``--quiet`` 의 유일한 출력 — id 만 한 줄에 하나.

    사람이 읽는 문장을 여기 섞으면 ``for id in $(… --quiet)`` 가 그것을 id 로
    읽는다. 빈 목록은 **아무것도** 출력하지 않는다.
    """
    for row in rows:
        print(row["id"])
    return 0


def _cmd_products_list(args: argparse.Namespace) -> int:
    rows = _fetch(_PRODUCTS_PATH)
    if rows is None:
        return 1
    if args.quiet:
        return _print_ids(rows)
    if not rows:
        print("No products yet. Create one in the app, or with `bsvibe_products_create` over MCP.")
        return 0
    for row in rows:
        # ⚠️ REST ``ProductResponse`` 의 필드만 읽는다. MCP ``bsvibe_products_list``
        # 는 ``execution_target`` 을 얹어 주지만 REST 는 아니다 — MCP 응답을 보고
        # 필드를 지으면 목록이 통째로 "-" 가 된다.
        status = row.get("bootstrap_status") or "-"
        print(f"{row['id']}  {row['slug']:<20} {status:<24} {row.get('name') or ''}")
    return 0


def _cmd_runs_list(args: argparse.Namespace) -> int:
    # ⚠️ 서버가 아는 파라미터만 보낸다. ``GET /api/v1/runs`` 는 ``limit`` 하나만
    # 받는다 — 모르는 쿼리는 **에러가 아니라 무시**되므로, 있지도 않은
    # ``--product`` 를 실어 보내면 필터가 걸린 것처럼 보이고 전체가 돌아온다.
    # (MCP ``bsvibe_runs_list`` 는 전체를 받아 클라이언트 측에서 거른다. REST 에
    # 같은 축을 여는 것은 별도 변경이다 — 이 PR 은 조회 표면만 연다.)
    params: dict[str, Any] = {}
    if args.limit:
        params["limit"] = args.limit
    rows = _fetch(_RUNS_PATH, params)
    if rows is None:
        return 1
    if args.quiet:
        return _print_ids(rows)
    if not rows:
        print("No runs yet.")
        return 0
    for row in rows:
        status = row.get("status") or "-"
        print(f"{row['id']}  {status:<14} {_one_line(str(row.get('intent') or ''))}")
    return 0


def _cmd_deliverables_list(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {}
    if args.run:
        params["run_id"] = args.run
    if args.limit:
        params["limit"] = args.limit
    rows = _fetch(_DELIVERABLES_PATH, params)
    if rows is None:
        return 1
    if args.quiet:
        return _print_ids(rows)
    if not rows:
        print("No deliverables yet.")
        return 0
    for row in rows:
        kind = row.get("deliverable_type") or "-"
        # ``verified`` 는 PASSED 검증이 실재할 때만 True 다(B4) — 행이 있다는
        # 사실만으로 초록을 주지 않는다. 목록에서도 그 구분이 보여야 한다.
        mark = "verified" if row.get("verified") else "unverified"
        print(f"{row['id']}  {kind:<16} {mark:<11} run={row.get('run_id')}")
    return 0


def _add_quiet(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print ONLY ids, one per line — safe for `for id in $(… --quiet)`.",
    )


def _add_product_surface_parsers(sub: Any) -> None:
    p_products = sub.add_parser("products", help="List products in this workspace.")
    products_sub = p_products.add_subparsers(dest="products_cmd", required=True)
    p_pl = products_sub.add_parser("list", help="List products.")
    _add_quiet(p_pl)
    p_pl.set_defaults(func=_cmd_products_list)

    p_runs = sub.add_parser("runs", help="List execution runs.")
    runs_sub = p_runs.add_subparsers(dest="runs_cmd", required=True)
    p_rl = runs_sub.add_parser("list", help="List runs, newest first.")
    p_rl.add_argument("--limit", type=int, default=None, help="How many to return.")
    _add_quiet(p_rl)
    p_rl.set_defaults(func=_cmd_runs_list)

    p_del = sub.add_parser("deliverables", help="List deliverables.")
    del_sub = p_del.add_subparsers(dest="deliverables_cmd", required=True)
    p_dl = del_sub.add_parser("list", help="List deliverables, newest first.")
    p_dl.add_argument("--run", default=None, help="Narrow to one run id.")
    p_dl.add_argument("--limit", type=int, default=None, help="How many to return.")
    _add_quiet(p_dl)
    p_dl.set_defaults(func=_cmd_deliverables_list)


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

    p_status = sub.add_parser(
        "status",
        help=(
            "Show sign-in status. Exit: 0 signed in, 1 not signed in, "
            "2 expired (re-login required), 3 expired (refresh token present)."
        ),
    )
    p_status.set_defaults(func=_cmd_status)

    _add_pat_parser(sub)
    _add_product_surface_parsers(sub)

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


def _format_staleness_report(report: StalenessReport, *, uid: int) -> str:
    """Render the diagnosis, and — when something is stale — the exact command
    that fixes it.

    ``uid`` is passed in rather than read here: the gui domain belongs to the
    user whose launchd owns these daemons, and this renderer stays pure for the
    same reason the process/git probes are injected.

    Naming the remedy is not a convenience. Measured 2026-08-26, the first real
    use of this command: it said "restart them." and stopped, so the reader had
    to source the command elsewhere — and the cheap guess, ``kill``, is the
    wrong one. launchd owns these daemons' lifecycle and the worker's identity
    is the token its service definition carries; ``kickstart -k`` is the
    restart that keeps both.
    """
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
            f"{len(report.stale)} stale daemon(s) running pre-HEAD code. "
            "Restart each through launchd — `kill` is not it, launchd owns their "
            "lifecycle and the worker's identity rides its service definition:"
        )
        lines.extend(f"  launchctl kickstart -k gui/{uid}/{d.label}" for d in report.stale)
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
    print(_format_staleness_report(report, uid=os.getuid()))
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
    "SessionState",
    "SessionStatus",
    "build_bsvibe_parser",
    "build_bsvibe_worker_parser",
    "evaluate_session",
    "run_bsvibe_cli",
    "run_bsvibe_worker_cli",
]
