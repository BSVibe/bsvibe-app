"""OpenFGA HTTP client (httpx async).

This wraps the small subset of OpenFGA endpoints the 4 products need:
- ``check`` — single permission decision
- ``list-objects`` — fan-out for ``"all <resource> I can <relation>"`` queries
- ``write`` — admin-only tuple writes (granting/revoking access)

Design notes
------------
* httpx async, default 3s timeout from settings (Auth_Design.md §10).
* All errors surface as `OpenFGAError` (or `OpenFGAAuthError` for 401/403).
  Phase 0 chooses to *raise* on 401 rather than re-issue the API token —
  the gateway-side retry is a follow-up (see lock-in §3 #16 follow-up).
* Built on :class:`bsvibe_core.http.HttpClientBase` for shared retry +
  redacted-logging infrastructure.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx
import structlog

from backend.shared.core.http import HttpClientBase

from .settings import Settings

logger = structlog.get_logger(__name__)


class OpenFGAError(RuntimeError):
    """OpenFGA returned a non-2xx response."""

    def __init__(self, status_code: int, body: Any) -> None:
        super().__init__(f"OpenFGA error {status_code}: {body!r}")
        self.status_code = status_code
        self.body = body


class OpenFGAAuthError(OpenFGAError):
    """OpenFGA rejected the API token (401/403)."""


class OpenFGAClient(HttpClientBase):
    """Small async wrapper around the OpenFGA HTTP API.

    Use as an async context manager so the underlying httpx client is closed:

        async with OpenFGAClient(settings) as fga:
            allowed = await fga.check("user:alice", "viewer", "project:p1")
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        headers: dict[str, str] = {"content-type": "application/json"}
        if settings.openfga_auth_token:
            headers["authorization"] = f"Bearer {settings.openfga_auth_token}"
        super().__init__(
            settings.openfga_api_url,
            http=http,
            timeout_s=settings.openfga_request_timeout_s,
            retries=0,
            headers=headers,
        )
        # Eager-build so callers can inspect ``self._http`` directly
        # (preserves the existing timeout-on-the-client contract).
        _ = self.http

    async def __aenter__(self) -> OpenFGAClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    @property
    def _store_path(self) -> str:
        return f"/stores/{self._settings.openfga_store_id}"

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self.post(path, json=payload)
        except httpx.HTTPError as exc:
            logger.error("openfga_request_failed", path=path, error=str(exc))
            raise OpenFGAError(0, {"network_error": str(exc)}) from exc

        if resp.status_code in (401, 403):
            logger.warning("openfga_auth_rejected", status=resp.status_code, path=path)
            raise OpenFGAAuthError(resp.status_code, _safe_json(resp))
        if resp.status_code >= 400:
            logger.error("openfga_error", status=resp.status_code, path=path)
            raise OpenFGAError(resp.status_code, _safe_json(resp))
        return _safe_json(resp)

    async def check(self, user: str, relation: str, object_: str) -> bool:
        """Return True iff `user` has `relation` on `object_`."""
        payload = {
            "tuple_key": {"user": user, "relation": relation, "object": object_},
            "authorization_model_id": self._settings.openfga_auth_model_id,
        }
        body = await self._post(f"{self._store_path}/check", payload)
        return bool(body.get("allowed", False))

    async def list_objects(self, user: str, relation: str, type_: str) -> list[str]:
        """Return all `type_:<id>` objects on which `user` has `relation`."""
        payload = {
            "user": user,
            "relation": relation,
            "type": type_,
            "authorization_model_id": self._settings.openfga_auth_model_id,
        }
        body = await self._post(f"{self._store_path}/list-objects", payload)
        objs = body.get("objects", [])
        return [str(o) for o in objs]

    async def write_tuple(self, user: str, relation: str, object_: str) -> None:
        """Admin-only: grant `user` `relation` on `object_`."""
        payload = {
            "writes": {"tuple_keys": [{"user": user, "relation": relation, "object": object_}]},
            "authorization_model_id": self._settings.openfga_auth_model_id,
        }
        await self._post(f"{self._store_path}/write", payload)

    async def delete_tuple(self, user: str, relation: str, object_: str) -> None:
        """Admin-only: revoke `user`'s `relation` on `object_`."""
        payload = {
            "deletes": {"tuple_keys": [{"user": user, "relation": relation, "object": object_}]},
            "authorization_model_id": self._settings.openfga_auth_model_id,
        }
        await self._post(f"{self._store_path}/write", payload)


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {"raw": resp.text}
    if isinstance(body, dict):
        return body
    return {"raw": body}
