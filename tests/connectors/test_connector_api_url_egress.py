"""커넥터 ``*_api_url`` 은 모델 계정과 같은 egress 검사를 통과해야 한다.

저장 시(REST·MCP)와 호출 직전(플러그인) 두 곳에서 막는다. DNS 는 주입해 네트워크를 쓰지 않는다.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from backend.api.v1 import connectors as rest
from backend.extensions.plugin.egress import EgressRefused, check_connector_config
from backend.mcp.api import ToolError
from backend.mcp.tools import connectors_tools as mcp
from plugin.github import plugin as github_plugin

pytestmark = pytest.mark.asyncio

_PUBLIC = "93.184.216.34"
_INTERNAL = [
    "http://127.0.0.1:8080",
    "http://10.0.0.5",
    "http://169.254.169.254/latest",
    "http://192.168.1.1",
]


async def _resolver(host: str) -> list[str]:
    return {"api.github.com": [_PUBLIC], "postgres": ["10.0.0.9"]}.get(host, ["10.0.0.9"])


@pytest.mark.parametrize("url", _INTERNAL)
async def test_rest_rejects_internal_api_url(url: str) -> None:
    with pytest.raises(HTTPException) as exc:
        await rest._check_api_urls({"github_api_url": url})
    assert exc.value.status_code == 422


async def test_rest_create_and_update_reject_before_touching_db() -> None:
    class _Session:
        async def get(self, *_a: Any) -> Any:
            return SimpleNamespace(workspace_id=ws, delivery_config={})

    ws = uuid.uuid4()
    update = rest.ConnectorUpdate(delivery_config={"slack_api_url": "http://10.0.0.5"})
    with pytest.raises(HTTPException) as exc:
        await rest.update_connector(uuid.uuid4(), update, ws, _Session())  # type: ignore[arg-type]
    assert exc.value.status_code == 422


@pytest.mark.parametrize("url", _INTERNAL)
async def test_mcp_rejects_internal_api_url(url: str) -> None:
    args = mcp.ConnectorsCreateInput(
        connector="github", signing_secret="s", delivery_config={"github_api_url": url}
    )
    with pytest.raises(ToolError):
        await mcp._h_create(args, None)  # type: ignore[arg-type]


async def test_mcp_set_delivery_config_rejects_internal(monkeypatch) -> None:
    async def _row(*_a: Any) -> Any:
        return SimpleNamespace(delivery_config={})

    monkeypatch.setattr(mcp, "_resolve_connector", _row)
    args = mcp.ConnectorsSetDeliveryConfigInput(
        connector_id=str(uuid.uuid4()), delivery_config={"telegram_api_url": "http://127.0.0.1"}
    )
    with pytest.raises(ToolError):
        await mcp._h_set_delivery_config(args, None)  # type: ignore[arg-type]


async def test_internal_service_name_rejected() -> None:
    with pytest.raises(EgressRefused):
        await check_connector_config(
            {"github_api_url": "http://postgres:5432"},
            resolver=_resolver,
            allowed_hosts=frozenset(),
        )


async def test_operator_allowlist_applies_like_model_accounts() -> None:
    await check_connector_config(
        {"github_api_url": "http://ghe.internal"},
        resolver=_resolver,
        allowed_hosts=frozenset({"ghe.internal"}),
    )


async def test_public_default_passes() -> None:
    await check_connector_config({}, resolver=_resolver, allowed_hosts=frozenset())
    await check_connector_config(
        {"github_api_url": "https://api.github.com", "repo": "a/b"},
        resolver=_resolver,
        allowed_hosts=frozenset(),
    )
    await rest._check_api_urls({"github_api_url": f"https://{_PUBLIC}"})


def _ctx(config: dict[str, Any]) -> Any:
    return SimpleNamespace(config=config, credentials={"token": "t"})


async def test_plugin_call_time_check_blocks_internal() -> None:
    with pytest.raises(EgressRefused):
        await github_plugin._client(_ctx({"github_api_url": "http://169.254.169.254"}))


async def test_plugin_default_url_still_builds_client() -> None:
    assert await github_plugin._client(_ctx({})) is not None
