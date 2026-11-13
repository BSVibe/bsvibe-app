"""GitHub 저장소 접근 확인 — 바인딩 생성과 웹훅 배분이 함께 쓰는 한 곳.

* :func:`verify_repo_access` — 커넥터 계정의 자격증명(앱 설치 토큰, 없으면 OAuth
  토큰)으로 GitHub API 에서 저장소를 조회한다. 접근이 확인되지 않으면
  :class:`RepoAccessError`.
* :func:`github_installation_id` — 계정에 기록된 GitHub App 설치 id. 웹훅의
  ``installation.id`` 와 대조하는 기준이다.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.auth.providers import get_provider
from backend.connectors.auth.resolve import resolve_connector_credentials
from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher, cipher_from_settings

_API_BASE = "https://api.github.com"
_HTTP_TIMEOUT = 10.0


class RepoAccessError(Exception):
    """계정이 저장소에 접근할 수 없다(권한 없음/없는 저장소/자격증명 거부)."""


class RepoAccessUnavailable(Exception):
    """GitHub 가 일시적으로 답하지 못했다(타임아웃·5xx·429). 접근 거부가 아니다 — 재시도 대상."""


_TRANSIENT_STATUSES = frozenset({429})


def _is_transient(status: int) -> bool:
    return status in _TRANSIENT_STATUSES or status >= 500  # noqa: PLR2004


def reject_server_owned_installation(
    connector: str, *, external_ref: str | None, delivery_config: dict[str, Any] | None
) -> None:
    """사용자 입력이 설치 id 를 주장하지 못하게 한다(``ValueError``).

    설치 id 는 설치 claim 이 서버에서 ``external_ref`` 에 기록한 값만 믿는다.
    ``delivery_config['installation_id']`` 와, github 커넥터를 직접 만들 때의 숫자
    ``external_ref`` 는 다른 테넌트의 설치 id 를 흉내 낼 수 있어 거절한다.
    """
    if delivery_config and "installation_id" in delivery_config:
        raise ValueError(
            "installation_id 는 서버가 설치 claim 때 기록하는 값이라 직접 설정할 수 없습니다"
        )
    if connector == "github" and external_ref is not None and external_ref.strip():
        raise ValueError(
            "github 커넥터의 external_ref 는 설치 claim 이 서버에서 기록하는 값이라 직접 설정할 수 없습니다"
        )


def github_installation_id(account: ConnectorAccountRow) -> str | None:
    """계정의 GitHub App 설치 id, 없으면 ``None``.

    서버가 설치 claim 때 기록한 ``external_ref`` (숫자) 에서만 읽는다.
    사용자가 쓸 수 있는 ``delivery_config`` 는 보지 않는다.
    """
    ref = (account.external_ref or "").strip()
    return ref if ref.isdigit() else None


def repo_slug(repo: str) -> str:
    """``owner/name`` 으로 정규화(URL·``.git`` 허용)."""
    s = repo.strip().removesuffix(".git")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        s = s.removeprefix(prefix)
    return s.strip("/")


async def _credential(session: AsyncSession, account: ConnectorAccountRow, cipher: Any) -> str:
    installation = github_installation_id(account)
    provider = get_provider("github")
    if installation and provider is not None and getattr(provider, "supports_service_token", False):
        token = await provider.service_token(install_ref=installation)
        return token.access_token
    creds = await resolve_connector_credentials(
        session, account=account, cipher=cipher or cipher_from_settings()
    )
    return creds["token"]


async def _repo_status(token: str, slug: str) -> int:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(
            f"{_API_BASE}/repos/{quote(slug, safe='/')}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
    return resp.status_code


async def verify_repo_access(
    session: AsyncSession,
    *,
    account: ConnectorAccountRow,
    resource_id: str,
    cipher: CredentialCipher | None = None,
) -> None:
    """github 계정이 ``resource_id`` 저장소를 실제로 조회할 수 있는지 확인한다.

    github 가 아닌 커넥터는 대상이 아니다(통과). 확인 실패는 모두
    :class:`RepoAccessError` 로 올린다 — 조용히 통과시키지 않는다.
    """
    if account.connector != "github":
        return
    slug = repo_slug(resource_id)
    try:
        token = await _credential(session, account, cipher)
        status = await _repo_status(token, slug)
    except httpx.HTTPError as exc:  # 타임아웃·연결 실패 — 접근 거부가 아니다
        raise RepoAccessUnavailable(
            f"GitHub 가 응답하지 않아 저장소 {slug} 접근을 확인하지 못했습니다. 잠시 후 다시 시도하세요 ({type(exc).__name__})"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — 자격증명/네트워크 어떤 실패든 접근 미확인
        raise RepoAccessError(
            f"GitHub 저장소 {slug} 접근을 확인하지 못했습니다 ({type(exc).__name__})"
        ) from exc
    if _is_transient(status):
        raise RepoAccessUnavailable(
            f"GitHub 가 일시적으로 실패해 저장소 {slug} 접근을 확인하지 못했습니다 (HTTP {status}). 잠시 후 다시 시도하세요"
        )
    if status != httpx.codes.OK:
        raise RepoAccessError(
            f"이 GitHub 연결은 저장소 {slug} 에 접근할 수 없습니다 (HTTP {status})"
        )


__all__ = [
    "RepoAccessError",
    "RepoAccessUnavailable",
    "github_installation_id",
    "reject_server_owned_installation",
    "repo_slug",
    "verify_repo_access",
]
