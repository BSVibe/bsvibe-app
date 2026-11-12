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

from collections.abc import Mapping
from typing import Any

from backend.config import get_settings
from backend.extensions.plugin.egress import (
    EgressRefused,
    Resolver,
    check_connector_config,
    check_endpoint,
    resolve_host,
    set_allowed_hosts_provider,
)

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

# Connector plugins share this check through ``backend.extensions.plugin.egress`` (they may not
# import the router); hand it the operator allow-list, read per call like the model path.
set_allowed_hosts_provider(lambda: frozenset(get_settings().llm_egress_allowed_hosts))


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


__all__ = [
    "EgressRefused",
    "check_completion_kwargs",
    "check_connector_config",
    "check_endpoint",
    "resolve_host",
    "Resolver",
]
