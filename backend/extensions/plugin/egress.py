"""Egress 검사의 의존성 없는 핵심 — 플러그인·MCP·REST·모델 경로가 같은 규칙을 쓴다.

플러그인은 ``backend.router`` / ``backend.shared`` 를 import 할 수 없으므로(import-linter),
공개 seam 인 ``backend.extensions.plugin`` 에 backend 의존이 없는 검사 모듈을 둔다.
운영자 허용 목록(``llm_egress_allowed_hosts``)은 이 모듈이 설정을 직접 읽지 않고,
``backend.router.egress`` 가 등록하는 공급자(:func:`set_allowed_hosts_provider`)로 받는다.
모델 계정과 같은 규칙 · 같은 허용 목록이다.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

Resolver = Callable[[str], Awaitable[list[str]]]

_CONNECTOR_URL_SUFFIX = "_api_url"

_allowed_hosts_provider: Callable[[], frozenset[str]] = frozenset


class EgressRefused(ValueError):
    """A model endpoint the server must not call. The message names the endpoint, not a key."""


def set_allowed_hosts_provider(provider: Callable[[], frozenset[str]]) -> None:
    """Register where the operator allow-list comes from (read per call)."""
    global _allowed_hosts_provider  # noqa: PLW0603 — one process-wide seam, set at import
    _allowed_hosts_provider = provider


async def resolve_host(host: str) -> list[str]:
    """Every address ``host`` resolves to (the system resolver, off the event loop)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _is_global(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


async def check_endpoint(
    url: str, *, resolver: Resolver, allowed_hosts: frozenset[str] = frozenset()
) -> None:
    """Refuse ``url`` unless it is http(s) and every address its host resolves to is global."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise EgressRefused(f"model endpoint must be an http(s) URL: {url!r}")
    host = parts.hostname
    if host in allowed_hosts:
        return
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await resolver(host)
        except (OSError, UnicodeError) as exc:
            raise EgressRefused(f"model endpoint host does not resolve: {host!r}") from exc
    if not addresses or not all(_is_global(a) for a in addresses):
        raise EgressRefused(
            f"model endpoint {host!r} is not a public address — the server does not call "
            "private, loopback or link-local networks on a workspace's behalf"
        )


async def check_connector_config(
    config: Mapping[str, Any] | None,
    *,
    resolver: Resolver | None = None,
    allowed_hosts: frozenset[str] | None = None,
) -> None:
    """Refuse a connector ``delivery_config`` whose ``*_api_url`` is not a public endpoint.

    Same rule and same operator allow-list (``llm_egress_allowed_hosts``) as model accounts.
    """
    if not config:
        return
    urls = [str(v) for k, v in config.items() if v and str(k).endswith(_CONNECTOR_URL_SUFFIX)]
    if not urls:
        return
    hosts = allowed_hosts if allowed_hosts is not None else _allowed_hosts_provider()
    resolve = resolver or resolve_host  # looked up per call so tests can substitute DNS
    for url in urls:
        await check_endpoint(url, resolver=resolve, allowed_hosts=hosts)


__all__ = [
    "EgressRefused",
    "Resolver",
    "check_connector_config",
    "check_endpoint",
    "resolve_host",
    "set_allowed_hosts_provider",
]
