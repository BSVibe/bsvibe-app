"""바인딩 생성은 GitHub 저장소 접근 확인을 거친다(REST·MCP)."""

from __future__ import annotations

import httpx
import pytest

from backend.connectors import github_access
from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher


def _account(**kw: object) -> ConnectorAccountRow:
    return ConnectorAccountRow(connector="github", delivery_config={}, **kw)  # type: ignore[arg-type]


def test_installation_id_only_from_server_recorded_external_ref() -> None:
    assert github_access.github_installation_id(_account(external_ref=None)) is None
    forged = ConnectorAccountRow(
        connector="github", external_ref=None, delivery_config={"installation_id": 77}
    )
    assert github_access.github_installation_id(forged) is None
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


# ── 실제 자격증명 선택 + 실제 _repo_status, HTTP 계층만 가짜 ──────────────────


class _NoOAuthSession:
    """OAuth 토큰 행이 없는 계정 — resolve_connector_credentials 가 서명 비밀(PAT)로 간다."""

    async def execute(self, *_a: object, **_k: object) -> object:
        class _R:
            def scalar_one_or_none(self) -> None:
                return None

        return _R()


def _fake_http(monkeypatch: pytest.MonkeyPatch, handler: object) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)  # type: ignore[operator]

    def _factory(*a: object, **k: object) -> httpx.AsyncClient:
        k["transport"] = httpx.MockTransport(_handle)
        return real(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(github_access.httpx, "AsyncClient", _factory)
    return seen


def _pat_account(cipher: CredentialCipher) -> ConnectorAccountRow:
    # 이 인스턴스 자신의 github 커넥터 모양: repo/base_branch 만, external_ref 없음, PAT.
    return ConnectorAccountRow(
        connector="github",
        external_ref=None,
        delivery_config={"repo": "o/r", "base_branch": "main"},
        signing_secret_ciphertext=cipher.encrypt("pat-token"),
    )


@pytest.mark.asyncio
async def test_real_credential_selection_sends_pat_and_passes_on_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cipher = _cipher()
    seen = _fake_http(monkeypatch, lambda _r: httpx.Response(200, json={}))
    await github_access.verify_repo_access(
        _NoOAuthSession(),  # type: ignore[arg-type]
        account=_pat_account(cipher),
        resource_id="https://github.com/o/r.git",
        cipher=cipher,
    )
    assert [str(r.url) for r in seen] == ["https://api.github.com/repos/o/r"]
    assert seen[0].headers["Authorization"] == "Bearer pat-token"


def _cipher() -> CredentialCipher:
    return CredentialCipher(b"k" * 32)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 403, 404])
async def test_real_path_denial_statuses_are_access_errors(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    cipher = _cipher()
    _fake_http(monkeypatch, lambda _r: httpx.Response(code))
    with pytest.raises(github_access.RepoAccessError):
        await github_access.verify_repo_access(
            _NoOAuthSession(),  # type: ignore[arg-type]
            account=_pat_account(cipher),
            resource_id="o/r",
            cipher=cipher,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [429, 500, 502, 503])
async def test_real_path_transient_statuses_are_unavailable_not_denied(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    cipher = _cipher()
    _fake_http(monkeypatch, lambda _r: httpx.Response(code))
    with pytest.raises(github_access.RepoAccessUnavailable):
        await github_access.verify_repo_access(
            _NoOAuthSession(),  # type: ignore[arg-type]
            account=_pat_account(cipher),
            resource_id="o/r",
            cipher=cipher,
        )


@pytest.mark.asyncio
async def test_real_path_timeout_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    cipher = _cipher()

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _fake_http(monkeypatch, _boom)
    with pytest.raises(github_access.RepoAccessUnavailable):
        await github_access.verify_repo_access(
            _NoOAuthSession(),  # type: ignore[arg-type]
            account=_pat_account(cipher),
            resource_id="o/r",
            cipher=cipher,
        )


def test_user_cannot_claim_an_installation_id() -> None:
    reject = github_access.reject_server_owned_installation
    with pytest.raises(ValueError):
        reject("github", external_ref=None, delivery_config={"installation_id": 1})
    with pytest.raises(ValueError):
        reject("github", external_ref="123", delivery_config={})
    with pytest.raises(ValueError):
        reject("github", external_ref="o/r", delivery_config={})
    reject("github", external_ref=None, delivery_config={"repo": "o/r"})
    reject("slack", external_ref="123", delivery_config={})
