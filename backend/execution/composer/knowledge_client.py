"""KnowledgeClient — wrapper around BSage knowledge primitives.

BSage is the project's long-term knowledge graph. BSNexus both reads
from it (to enrich prompts with prior context) and writes back to it
(to index run outputs so future projects can find them).

Read endpoints:
- ``GET  /api/knowledge/search?q=…&limit=…``
- ``GET  /api/vault/file?path=...``
- ``GET  /api/vault/backlinks?path=...``

Write endpoints:
- ``POST /api/webhooks/bsnexus-input`` — raw deliverable payload. The
  ``bsnexus-input`` plugin writes it as a seed; BSage's AgentLoop
  seed-refiner derives a concise title (under 30 chars) + refined
  content + tags via LLM. Matches the pattern every other BSage input
  plugin follows — BSNexus sends raw run output, BSage owns titling.
- ``POST /api/knowledge/decisions`` — structured decision record
  (still pre-titled; decisions carry a human-authored question).

``NoopKnowledgeClient`` is the fallback when BSage is disabled or
unreachable for the tenant — search/fetch/backlinks return empty,
writes return None. PromptAssembler and publish_run_output still
produce a valid (degraded) result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import structlog

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.core.clients
# from backend.src.core.clients import BaseServiceClient
# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.core.integrations.config
# from backend.src.core.integrations.config import ProviderConfig

if TYPE_CHECKING:
    pass
    # TODO(bundle-x-integration): out-of-scope source dep -- backend.src.core.service_auth
#     from backend.src.core.service_auth import ServiceJWTMinter

logger = structlog.get_logger(__name__)

_USER_AGENT = "BSNexus/0.2 (+https://nexus.bsvibe.dev)"


@dataclass(frozen=True)
class KnowledgeFragment:
    """One retrieved piece of knowledge from BSage."""

    path: str
    title: str
    excerpt: str
    score: float
    extra: dict = field(default_factory=dict)

    def to_ref(self) -> dict:
        """Serialize for composition_snapshots.context_doc_refs."""
        return {
            "path": self.path,
            "title": self.title,
            "score": self.score,
            "excerpt_hash": _hash_text(self.excerpt),
        }


def _hash_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class KnowledgeEntryRef:
    """Reference to a knowledge entry that was indexed in BSage."""

    id: str
    path: str


class KnowledgeClient(Protocol):
    """Protocol for knowledge retrieval + indexing backends.

    Implementations must NOT raise on transient failures — return empty
    or None so the calling code can always complete its main job
    (composition / deliverable creation / decision resolution) even if
    BSage is down.
    """

    async def search(self, intent: str, *, top_k: int = 10) -> list[KnowledgeFragment]: ...

    async def fetch(self, path: str) -> str | None: ...

    async def backlinks(self, path: str) -> list[str]: ...

    async def index(
        self,
        *,
        payload: dict,
        auth_token: str | None = None,
    ) -> KnowledgeEntryRef | None: ...

    async def record_decision(
        self,
        *,
        title: str,
        decision: str,
        reasoning: str,
        alternatives: list[str] | None = None,
        context: str = "",
        tags: list[str] | None = None,
        source: str = "bsnexus",
        auth_token: str | None = None,
    ) -> KnowledgeEntryRef | None: ...


class NoopKnowledgeClient:
    """Fallback when BSage is disabled or unreachable.

    All methods are no-ops. The resulting composition gets
    ``source="local"`` so the Inside panel can surface degraded mode.
    """

    async def search(self, intent: str, *, top_k: int = 10) -> list[KnowledgeFragment]:
        return []

    async def fetch(self, path: str) -> str | None:
        return None

    async def backlinks(self, path: str) -> list[str]:
        return []

    async def index(
        self,
        *,
        payload: dict,
        auth_token: str | None = None,
    ) -> KnowledgeEntryRef | None:
        return None

    async def record_decision(
        self,
        *,
        title: str,
        decision: str,
        reasoning: str,
        alternatives: list[str] | None = None,
        context: str = "",
        tags: list[str] | None = None,
        source: str = "bsnexus",
        auth_token: str | None = None,
    ) -> KnowledgeEntryRef | None:
        return None


class BSageKnowledgeClient:
    """HTTP client against BSage's existing endpoints.

    Composes ``BaseServiceClient`` for the shared Bearer + UA + timeout
    + fail-soft scaffolding (S2-1-X). The ``auth_provider`` closure
    decides per-call which Bearer to use; in Phase A this is the
    tenant's static api_key (or a forwarded user JWT), in Phase 0 P0.7
    it will be a service-JWT minter — adapter code does not change.

    Public attribute ``_headers`` is preserved as a snapshot of the
    resolved headers so existing internal-state tests keep passing
    while we migrate. Live requests build headers fresh from the
    auth_provider so swap-at-runtime works.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        auth_token: str | None = None,
        timeout_s: float = 3.0,
    ):
        # Auth precedence: per-instance ``auth_token`` (forwarded user
        # JWT) wins over the tenant's static ``api_key``. With neither,
        # BSage's @protected routes return 401 — the resolved closure
        # surfaces that as an empty-string token so no malformed
        # ``Bearer `` header is sent.
        self._instance_token = auth_token or api_key or ""
        self._base = BaseServiceClient(
            base_url=base_url,
            auth_provider=self._default_auth_provider,
            user_agent=_USER_AGENT,
            timeout_s=timeout_s,
        )
        # Snapshot for backward-compat tests inspecting ``_headers``.
        # The live request path doesn't read this attribute — it
        # reconstructs headers via ``BaseServiceClient`` so a caller
        # who later calls ``set_auth_provider`` is honored.
        self._headers: dict[str, str] = {"User-Agent": _USER_AGENT}
        if self._instance_token:
            self._headers["Authorization"] = f"Bearer {self._instance_token}"

    def _default_auth_provider(self) -> str:
        return self._instance_token

    def _headers_with_token(self, auth_token: str | None) -> dict[str, str]:
        """Return request headers, preferring a caller-supplied Bearer
        token over the instance default.
        """
        if not auth_token:
            return self._headers
        return {**self._headers, "Authorization": f"Bearer {auth_token}"}

    async def search(self, intent: str, *, top_k: int = 10) -> list[KnowledgeFragment]:
        resp = await self._base.safe_request(
            "GET",
            "/api/knowledge/search",
            event="bsage_search_failed",
            params={"q": intent, "limit": top_k},
        )
        if resp is None:
            return []
        try:
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — fail-soft on parse
            logger.warning("bsage_search_failed", error=str(exc), reason="response_parse")
            return []

        # BSage's SearchResult fields: title, path, preview, score, tags.
        # Earlier/alternate payloads may use content/excerpt instead of preview.
        known_keys = {"path", "title", "preview", "excerpt", "content", "score"}
        return [
            KnowledgeFragment(
                path=hit.get("path", ""),
                title=hit.get("title", hit.get("path", "")),
                excerpt=hit.get("preview") or hit.get("excerpt") or hit.get("content", ""),
                score=float(hit.get("score", 0.0)),
                extra={k: v for k, v in hit.items() if k not in known_keys},
            )
            for hit in payload.get("results", [])
        ]

    async def fetch(self, path: str) -> str | None:
        resp = await self._base.safe_request(
            "GET",
            "/api/vault/file",
            event="bsage_fetch_failed",
            params={"path": path},
        )
        if resp is None:
            return None
        if resp.status_code == 404:
            return None
        try:
            resp.raise_for_status()
            return resp.json().get("content")
        except Exception as exc:  # noqa: BLE001 — fail-soft on parse
            logger.warning("bsage_fetch_failed", path=path, error=str(exc), reason="response_parse")
            return None

    async def backlinks(self, path: str) -> list[str]:
        resp = await self._base.safe_request(
            "GET",
            "/api/vault/backlinks",
            event="bsage_backlinks_failed",
            params={"path": path},
        )
        if resp is None:
            return []
        try:
            resp.raise_for_status()
            return list(resp.json().get("backlinks", []))
        except Exception as exc:  # noqa: BLE001 — fail-soft on parse
            logger.warning(
                "bsage_backlinks_failed", path=path, error=str(exc), reason="response_parse"
            )
            return []

    async def index(
        self,
        *,
        payload: dict,
        auth_token: str | None = None,
    ) -> KnowledgeEntryRef | None:
        """Forward a run payload to BSage's bsnexus-input webhook.

        The webhook response confirms the seed was accepted but does not
        yet include the final note path — BSage's seed-refiner runs
        asynchronously to derive a title + refined content. Callers that
        just want "did BSage accept it?" check for a non-None ref.
        """
        resp = await self._base.safe_request(
            "POST",
            "/api/webhooks/bsnexus-input",
            event="bsage_index_failed",
            json=payload,
            headers=self._headers_with_token(auth_token) if auth_token else None,
        )
        if resp is None:
            return None
        try:
            resp.raise_for_status()
            body = resp.json() if resp.content else {}
        except Exception as exc:  # noqa: BLE001 — fail-soft
            logger.warning("bsage_index_failed", error=str(exc), reason="response_parse")
            return None
        # Webhook returns {"plugin": "bsnexus-input", "results": [...]}.
        # Surface plugin acceptance as an anonymous ref — BSage owns the
        # actual note id/path once the refiner runs.
        return KnowledgeEntryRef(
            id=str(body.get("plugin", "bsnexus-input")),
            path="",
        )

    async def record_decision(
        self,
        *,
        title: str,
        decision: str,
        reasoning: str,
        alternatives: list[str] | None = None,
        context: str = "",
        tags: list[str] | None = None,
        source: str = "bsnexus",
        auth_token: str | None = None,
    ) -> KnowledgeEntryRef | None:
        body = {
            "title": title,
            "decision": decision,
            "reasoning": reasoning,
            "alternatives": list(alternatives or []),
            "context": context,
            "tags": list(tags or []),
            "source": source,
        }
        resp = await self._base.safe_request(
            "POST",
            "/api/knowledge/decisions",
            event="bsage_decision_failed",
            json=body,
            headers=self._headers_with_token(auth_token) if auth_token else None,
        )
        if resp is None:
            logger.warning("bsage_decision_failed", title=title[:60], reason="transport")
            return None
        try:
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — fail-soft
            logger.warning(
                "bsage_decision_failed", title=title[:60], error=str(exc), reason="response_parse"
            )
            return None
        return KnowledgeEntryRef(
            id=str(payload.get("id", "")),
            path=str(payload.get("path", "")),
        )


def resolve_knowledge_client(
    cfg: ProviderConfig | None,
    *,
    auth_token: str | None = None,
    service_jwt_minter: "ServiceJWTMinter | None" = None,
    tenant_id: str | None = None,
) -> KnowledgeClient:
    """Factory: return BSage client when configured, Noop otherwise.

    Auth precedence (P0.7 onwards):
      1. ``service_jwt_minter`` + ``tenant_id`` — minted service JWT
         (Lockin §3 #16, audience-scoped). The legacy static api_key
         on the integration row is NOT consulted when the minter is
         in play.
      2. ``auth_token`` — forwarded user SSO JWT (founder same-account
         attribution).
      3. ``cfg.api_key`` — legacy static key (Phase A drop).
    """
    if cfg is None or not cfg.enabled or not cfg.base_url:
        return NoopKnowledgeClient()

    # P0.7 — closure-only swap (Decision #15). The adapter signature
    # is unchanged; we just override the auth_provider on the
    # underlying BaseServiceClient after construction.
    if service_jwt_minter is not None and tenant_id is not None:
        client = BSageKnowledgeClient(cfg.base_url, api_key=None, auth_token=None)
        client._base.set_auth_provider(
            service_jwt_minter.make_auth_provider(
                audience="bsage",
                tenant_id=tenant_id,
                scope=["bsage:read"],
            )
        )
        return client

    return BSageKnowledgeClient(cfg.base_url, cfg.api_key, auth_token=auth_token)
