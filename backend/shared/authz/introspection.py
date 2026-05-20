"""RFC 7662 OAuth2 Token Introspection client.

Used by resource servers (BSGateway, BSAge, …) to validate opaque
``bsv_sk_*`` tokens against BSVibe-Auth's introspection endpoint. Returns
``IntrospectionResponse(active=False)`` on any HTTP / network / parse error
so callers can treat unreachable auth-server as "token rejected" without
leaking the failure mode through the auth path.

Token values are NEVER logged.

Built on :class:`bsvibe_core.http.HttpClientBase` for shared retry +
redacted-logging infrastructure. The Basic-auth header is masked in logs.
"""

from __future__ import annotations

import base64

import httpx
import structlog
from pydantic import ValidationError

from backend.shared.core.http import HttpClientBase

from .types import IntrospectionResponse

logger = structlog.get_logger(__name__)


class IntrospectionClient(HttpClientBase):
    """RFC 7662 introspection client.

    Pass a shared ``httpx.AsyncClient`` for connection-pool reuse in
    production; if omitted, the underlying client is built lazily on
    first introspect() call and owned by this instance.
    """

    def __init__(
        self,
        introspection_url: str,
        client_id: str,
        client_secret: str,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        if not introspection_url:
            raise ValueError("introspection_url must not be empty")
        if not client_id or not client_secret:
            raise ValueError("client_id and client_secret must be non-empty")

        self._url = introspection_url
        self._client_id = client_id
        self._client_secret = client_secret

        raw = f"{client_id}:{client_secret}".encode()
        basic_auth = "Basic " + base64.b64encode(raw).decode()
        super().__init__(
            "",
            http=http,
            timeout_s=timeout_s,
            retries=0,
            headers={
                "Authorization": basic_auth,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )

    async def introspect(self, token: str) -> IntrospectionResponse:
        body = {"token": token, "token_type_hint": "access_token"}

        try:
            resp = await self.post(self._url, data=body)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "introspection_http_error",
                status=exc.response.status_code,
                url=self._url,
            )
            return IntrospectionResponse(active=False)
        except (httpx.RequestError, ValueError) as exc:
            logger.warning(
                "introspection_request_failed",
                error=str(exc),
                url=self._url,
            )
            return IntrospectionResponse(active=False)

        try:
            return IntrospectionResponse.model_validate(data)
        except ValidationError as exc:
            logger.warning(
                "introspection_parse_failed",
                error=str(exc),
                url=self._url,
            )
            return IntrospectionResponse(active=False)
