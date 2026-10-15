"""Where a tenant-configured completion may be sent: public addresses only.

``ModelAccount.api_base`` and ``extra_params`` are whatever the workspace typed, and both
end up as kwargs to ``litellm.acompletion`` — a request the BACKEND makes, from inside the
operator's network. Unchecked, a tenant could aim it at ``postgres:5432``, the metadata
address, or the operator's own Ollama, and read the reply (or the error body) back.

:func:`check_completion_kwargs` reads the FINAL kwargs — after ``extra_params`` is merged,
which is merged last and so can replace ``api_base`` — and refuses unless every endpoint in
them resolves, entirely, to global addresses. A model whose LiteLLM default is the local
loopback (Ollama, vLLM, …) must name an endpoint, or it would call the server's own.

What this does NOT close: the name is resolved here and again by the HTTP client, so a
DNS answer that changes between the two (rebinding) still gets through. Pinning the address
would need the HTTP client's cooperation; an egress firewall on the backend container is the
complete answer. This is the application-layer floor.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

#: The kwargs that name where LiteLLM sends the request.
_ENDPOINT_KEYS = frozenset({"api_base", "base_url"})
_ENDPOINT_SUFFIXES = ("_endpoint", "_api_base", "_base_url")

#: Providers whose LiteLLM default endpoint is the local host.
_LOOPBACK_DEFAULT_PREFIXES = (
    "ollama/",
    "ollama_chat/",
    "lm_studio/",
    "hosted_vllm/",
    "vllm/",
    "llamafile/",
)

Resolver = Callable[[str], Awaitable[list[str]]]


class EgressRefused(ValueError):
    """A model endpoint the server must not call. The message names the endpoint, not a key."""


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


def _endpoints(kwargs: Mapping[str, Any]) -> list[str]:
    return [
        str(value)
        for key, value in kwargs.items()
        if value and (key in _ENDPOINT_KEYS or key.endswith(_ENDPOINT_SUFFIXES))
    ]


async def check_completion_kwargs(
    kwargs: Mapping[str, Any],
    *,
    resolver: Resolver = resolve_host,
    allowed_hosts: frozenset[str] = frozenset(),
) -> None:
    """Refuse a completion whose final kwargs would send it anywhere but the public internet."""
    endpoints = _endpoints(kwargs)
    model = str(kwargs.get("model") or "")
    if not endpoints and model.startswith(_LOOPBACK_DEFAULT_PREFIXES):
        raise EgressRefused(
            f"model {model!r} needs an explicit public api_base — without one it would call "
            "the server's own loopback"
        )
    for url in endpoints:
        await check_endpoint(url, resolver=resolver, allowed_hosts=allowed_hosts)


__all__ = ["EgressRefused", "check_completion_kwargs", "check_endpoint", "resolve_host"]
