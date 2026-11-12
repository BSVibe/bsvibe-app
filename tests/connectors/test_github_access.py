"""바인딩 생성은 GitHub 저장소 접근 확인을 거친다(REST·MCP)."""

from __future__ import annotations

import pytest

from backend.connectors import github_access
from backend.connectors.db import ConnectorAccountRow


def _account(**kw: object) -> ConnectorAccountRow:
    return ConnectorAccountRow(connector="github", delivery_config={}, **kw)  # type: ignore[arg-type]


def test_installation_id_from_delivery_config_or_numeric_ref() -> None:
    assert github_access.github_installation_id(_account(external_ref=None)) is None
    assert github_access.github_installation_id(_account(external_ref="o/r")) is None
    assert github_access.github_installation_id(_account(external_ref="77")) == "77"


@pytest.mark.asyncio
async def test_inaccessible_repo_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _cred(*_a: object, **_k: object) -> str:
        return "tok"

    async def _status(_t: str, _s: str) -> int:
        return 404

    monkeypatch.setattr(github_access, "_credential", _cred)
    monkeypatch.setattr(github_access, "_repo_status", _status)
    with pytest.raises(github_access.RepoAccessError):
        await github_access.verify_repo_access(
            None,
            account=_account(external_ref=None),
            resource_id="o/r",
            cipher=None,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_accessible_repo_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _cred(*_a: object, **_k: object) -> str:
        return "tok"

    async def _status(_t: str, _s: str) -> int:
        return 200

    monkeypatch.setattr(github_access, "_credential", _cred)
    monkeypatch.setattr(github_access, "_repo_status", _status)
    await github_access.verify_repo_access(
        None,
        account=_account(external_ref=None),
        resource_id="o/r",
        cipher=None,  # type: ignore[arg-type]
    )
