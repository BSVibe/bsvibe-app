"""/api/v1/connectors — founder-facing ConnectorAccount CRUD (Workflow §11.2).

Lets a founder register, list, and revoke a per-workspace inbound connector
binding and learn the webhook URL to paste into the external service
(GitHub / Slack / Telegram / Discord). This is the missing front door for the
PUBLIC ingress added in §11.2 (``POST /api/webhooks/{connector}/{token}``):
without a way to CREATE a ``connector_accounts`` row there is no token for an
external provider to call.

Capability handling (mirrors :mod:`backend.api.v1.accounts`' encrypt-on-write /
never-return-secret pattern):

* On create the server mints an unguessable ``webhook_token``
  (``secrets.token_urlsafe(32)``) and encrypts ``signing_secret`` via
  :class:`backend.router.accounts.crypto.CredentialCipher` — the plaintext secret
  never touches disk and is never returned over the API.
* The ``webhook_token`` (and the full webhook URL built from it) is returned
  ONLY in the create response, exactly once, like an API key. List responses
  expose a masked hint (last 4 chars) — never the full token, which is itself
  a capability (it is half of the ingress auth).

The allowed connector set is the derived connector catalog
(:func:`backend.connectors.catalog.get_connector_catalog`, INV-1 single source
of truth). A connector is creatable when it appears in the catalog AND is
``user_connectable`` — so suppressed connectors (linear / trello) are rejected
at the front door even though their outbound builders still deliver existing
bindings.

Lift B — inbound import surface. Connectors whose import path is a *pull*
(scan an Obsidian vault, parse a Claude/GPT conversations.json export,
walk a Notion workspace) get a third entry point:
:func:`POST /api/v1/connectors/{id}/import`. The route resolves the bound
connector, looks up its plugin's import action via the catalog's
``import_action``, and dispatches through :class:`PluginRunner` with the bound
``delivery_config`` injected into the action's :class:`SkillContext.config` —
so the founder UI can trigger a bulk import without re-typing the binding's
config every time.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id

# Reuse the ingress's cipher dependency so the create-side encrypt and the
# webhook-side decrypt share one (test-overridable) cipher.
from backend.api.webhooks import get_credential_cipher
from backend.connectors.auth.db import ConnectorOAuthTokenRow
from backend.connectors.auth.resolve import resolve_connector_credentials
from backend.connectors.catalog import ConnectorInfo, get_connector_catalog, legacy_kind
from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.base import PluginMeta, PluginRunError
from backend.extensions.plugin.context import SkillContext
from backend.extensions.plugin.runner import PluginRunner
from backend.router.accounts.crypto import CredentialCipher

logger = structlog.get_logger(__name__)

router = APIRouter()

# Length of the minted capability. token_urlsafe(32) yields ~43 base64url chars.
_TOKEN_BYTES = 32

# Audit event names emitted on import trigger / completion. The strings
# follow the existing ``audit.<domain>.<action>`` namespacing so log
# searches can route on them deterministically.
_AUDIT_IMPORT_TRIGGERED = "audit.connector.import_triggered"
_AUDIT_IMPORT_COMPLETED = "audit.connector.import_completed"
_AUDIT_IMPORT_FAILED = "audit.connector.import_failed"


def _webhook_url(connector: str, webhook_token: str) -> str:
    """The path an external provider POSTs to (mounted under ``/api``)."""
    return f"/api/webhooks/{connector}/{webhook_token}"


class ConnectorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector: str = Field(min_length=1, max_length=64)
    signing_secret: str = Field(min_length=1, max_length=1024)
    external_ref: str | None = Field(default=None, max_length=255)
    # Outbound delivery target binding (Workflow §12.5 #8). For a connector
    # with an ``@p.outbound`` this carries the STABLE routing fields it needs to
    # deliver a verified Deliverable OUT (e.g. notion ``{"parent_page_id": …}``).
    # Routing is founder-set config — never derived from LLM/work output.
    #
    # For inbound-only connectors (obsidian / claude / gpt) the same dict
    # carries the IMPORT binding (e.g. ``{"vault_path": "/…"}``,
    # ``{"export_path": "/…"}``). One uniform shape so the wire stays
    # stable; the plugin's import action falls back to its keys.
    delivery_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("connector")
    @classmethod
    def _known_connector(cls, v: str) -> str:
        # A connector is creatable when it is in the derived catalog AND is
        # user-connectable (INV-1). Suppressed connectors (linear / trello)
        # are IN the catalog — their outbound builders keep delivering
        # existing bindings — but are NOT creatable via this front door,
        # so they read as "unknown" to the founder API (product decision).
        info = get_connector_catalog().get(v)
        if info is None or not info.user_connectable:
            raise ValueError(f"unknown connector {v!r}")
        return v


class ConnectorCreated(BaseModel):
    """Create response — the ONLY place the webhook_token + URL are shown."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    connector: str
    external_ref: str | None
    is_active: bool
    created_at: datetime
    delivery_config: dict[str, Any]
    webhook_token: str
    webhook_url: str
    # INV-1 — the three orthogonal capability flags (derived from the catalog)
    # so the PWA can branch its create form + show / hide the Import-now action
    # without re-asking the backend. Replaces the retired inbound/outbound/both
    # ``kind`` enum.
    outbound: bool
    importable: bool
    webhook_trigger: bool
    # Backward-compat for the pre-catalog PWA (reads ``connector.kind`` to
    # decide whether to show "Import now"); derived from the flags, removed
    # once PR-8 migrates the connector-row UI to them (INV-1 expand/contract).
    kind: str | None


class ConnectorOut(BaseModel):
    """List response — never the secret, never the full token."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    connector: str
    external_ref: str | None
    is_active: bool
    created_at: datetime
    delivery_config: dict[str, Any]
    token_hint: str
    # INV-1 — capability flags (derived from the catalog) so the row UI can
    # branch on outbound / importable / webhook_trigger, plus the last-import
    # telemetry the import endpoint stamps after each run.
    outbound: bool
    importable: bool
    webhook_trigger: bool
    # Backward-compat for the pre-catalog PWA (reads ``connector.kind`` to
    # decide whether to show "Import now"); derived from the flags, removed
    # once PR-8 migrates the connector-row UI to them (INV-1 expand/contract).
    kind: str | None
    last_import_at: datetime | None
    last_import_count: int | None
    # Lift 1 — for oauth2 connectors (github, …): the connected account's
    # ``@login`` / workspace name when an OAuth token is bound, else None.
    # ``None`` means "not connected via OAuth" so the UI shows "Connect with X"
    # instead of "Connected as …". Never the token itself.
    oauth_account_label: str | None = None
    # Lift E46 — True when the bound OAuth token row is in the
    # ``needs_reauth`` state (the refresh-token endpoint rejected the
    # last refresh attempt, ``resolve_connector_credentials`` raised
    # :class:`ConnectorReauthRequired`, the row was flipped). The PWA
    # renders a "Reconnect" CTA instead of the steady-state "Connected"
    # badge so the founder sees the credential is dead instead of
    # waiting for the next dispatch to fail silently.
    needs_reauth: bool = False


class ConnectorImportResult(BaseModel):
    """Response shape of :func:`POST /api/v1/connectors/{id}/import`."""

    model_config = ConfigDict(extra="forbid")

    imported_count: int
    last_import_at: datetime
    # The connector's import action returns its own summary dict
    # (notes_count / scanned_count / skipped / region for obsidian;
    # conversations_count / messages_count / skipped / region for
    # claude/gpt; pages_count / blocks_count / skipped / region for
    # notion). We surface it under ``detail`` unchanged so the PWA can
    # show a connector-specific breakdown without the backend re-shaping
    # per-connector counts.
    detail: dict[str, Any]


class CatalogEntry(BaseModel):
    """One founder-visible connector in the derived catalog (INV-1).

    Carries the capability flags the PWA branches on when rendering the
    create form + the "Import now" button, plus the artifact types an
    outbound connector delivers and the import action an importable one runs.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    outbound: bool
    importable: bool
    webhook_trigger: bool
    artifact_types: list[str]
    import_action: str | None


class ConnectorCatalog(BaseModel):
    """The founder-visible connector catalog (user-connectable entries only)."""

    model_config = ConfigDict(extra="forbid")

    connectors: list[CatalogEntry]


def _token_hint(webhook_token: str) -> str:
    """Last 4 chars only — enough to recognise, not enough to use."""
    return f"...{webhook_token[-4:]}"


def _capabilities(connector: str) -> ConnectorInfo | None:
    """The derived capability flags for ``connector`` (``None`` if unknown)."""
    return get_connector_catalog().get(connector)


def _row_to_out(
    row: ConnectorAccountRow,
    *,
    oauth_account_label: str | None = None,
    needs_reauth: bool = False,
) -> ConnectorOut:
    info = _capabilities(row.connector)
    return ConnectorOut(
        id=row.id,
        connector=row.connector,
        external_ref=row.external_ref,
        is_active=row.is_active,
        created_at=row.created_at,
        delivery_config=row.delivery_config,
        token_hint=_token_hint(row.webhook_token),
        outbound=bool(info and info.outbound),
        importable=bool(info and info.importable),
        webhook_trigger=bool(info and info.webhook_trigger),
        kind=legacy_kind(info) if info else None,
        last_import_at=row.last_import_at,
        last_import_count=row.last_import_count,
        oauth_account_label=oauth_account_label,
        needs_reauth=needs_reauth,
    )


@router.get("")
async def list_connectors(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ConnectorOut]:
    rows = (
        (
            await session.execute(
                select(ConnectorAccountRow)
                .where(ConnectorAccountRow.workspace_id == workspace_id)
                .order_by(ConnectorAccountRow.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    # One query for every bound OAuth token's account label + status
    # (Lift E46), keyed by binding — so the list can show "Connected as
    # @login" / "Reconnect" without an N+1 fan-out.
    token_rows = (
        await session.execute(
            select(
                ConnectorOAuthTokenRow.connector_account_id,
                ConnectorOAuthTokenRow.account_label,
                ConnectorOAuthTokenRow.status,
            ).where(ConnectorOAuthTokenRow.connector_account_id.in_([r.id for r in rows]))
        )
    ).all()
    labels: dict[uuid.UUID, str | None] = {
        account_id: label for account_id, label, _status in token_rows
    }
    needs_reauth_by_id: dict[uuid.UUID, bool] = {
        account_id: status == "needs_reauth" for account_id, _label, status in token_rows
    }
    return [
        _row_to_out(
            r,
            oauth_account_label=labels.get(r.id),
            needs_reauth=needs_reauth_by_id.get(r.id, False),
        )
        for r in rows
    ]


@router.get("/catalog")
async def get_catalog() -> ConnectorCatalog:
    """The founder-visible connector catalog (INV-1, derived from PluginMeta).

    Returns only ``user_connectable`` entries — suppressed connectors
    (linear / trello) are naturally absent — each carrying its capability
    flags so the PWA can branch the create form (outbound target config vs
    import binding) and show / hide the "Import now" button without a second
    hardcoded map.
    """
    entries = [
        CatalogEntry(
            name=info.name,
            outbound=info.outbound,
            importable=info.importable,
            webhook_trigger=info.webhook_trigger,
            artifact_types=list(info.artifact_types),
            import_action=info.import_action,
        )
        for info in sorted(get_connector_catalog().values(), key=lambda i: i.name)
        if info.user_connectable
    ]
    return ConnectorCatalog(connectors=entries)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_connector(
    payload: ConnectorCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> ConnectorCreated:
    webhook_token = secrets.token_urlsafe(_TOKEN_BYTES)
    row = ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        connector=payload.connector,
        webhook_token=webhook_token,
        signing_secret_ciphertext=cipher.encrypt(payload.signing_secret),
        external_ref=payload.external_ref,
        delivery_config=payload.delivery_config,
        is_active=True,
    )
    session.add(row)
    await session.commit()
    info = _capabilities(row.connector)
    return ConnectorCreated(
        id=row.id,
        connector=row.connector,
        external_ref=row.external_ref,
        is_active=row.is_active,
        created_at=row.created_at,
        delivery_config=row.delivery_config,
        webhook_token=webhook_token,
        webhook_url=_webhook_url(row.connector, webhook_token),
        outbound=bool(info and info.outbound),
        importable=bool(info and info.importable),
        webhook_trigger=bool(info and info.webhook_trigger),
        kind=legacy_kind(info) if info else None,
    )


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_connector(
    connector_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """Soft-revoke: flip ``is_active`` False. The ingress already 404s on it."""
    row = await session.get(ConnectorAccountRow, connector_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"connector {connector_id} not found",
        )
    row.is_active = False
    await session.commit()


# ── inbound import (Lift B) ─────────────────────────────────────────────────


class ImportDispatcher:
    """The runtime hand-off that actually calls a plugin's import action.

    Resolves the plugin by connector name in a pre-loaded registry, builds
    a :class:`SkillContext` carrying the bound ``delivery_config`` +
    decrypted secret + workspace-scoped knowledge garden, and dispatches
    through :class:`PluginRunner.dispatch_action`. Tests override the
    :func:`get_import_dispatcher` dependency with an in-test fake so a
    unit run never touches the loader / filesystem / KMS.
    """

    def __init__(
        self,
        *,
        plugins_by_name: dict[str, PluginMeta],
        cipher: CredentialCipher,
        knowledge_factory: Callable[[uuid.UUID], Any],
        runner: PluginRunner | None = None,
    ) -> None:
        self._plugins_by_name = plugins_by_name
        self._cipher = cipher
        self._knowledge_factory = knowledge_factory
        self._runner = runner or PluginRunner()

    async def import_for(
        self,
        *,
        row: ConnectorAccountRow,
        workspace_id: uuid.UUID,
        session: AsyncSession,
    ) -> dict[str, Any]:
        meta = self._plugins_by_name.get(row.connector)
        if meta is None:
            raise PluginRunError(f"import: plugin {row.connector!r} not loaded")
        # The import-trigger action is declared on the plugin itself (PR-5,
        # ``PluginMeta.import_action_name``) — the same value the catalog's
        # ``import_action`` surfaces. The importable gate rejects earlier;
        # this is a defensive guard.
        action_name = meta.import_action_name
        if action_name is None:
            raise PluginRunError(f"import: no bulk-import action for connector {row.connector!r}")

        # The API credential carried under the ``token`` slot the connector-
        # action bridge uses (Notion's import reads it; obsidian/claude/gpt
        # ignore it). Routed through the unified resolver: an OAuth token if
        # one is bound, else the legacy signing secret (behavior-preserving
        # for every connector that hasn't been moved to OAuth yet).
        credentials: dict[str, Any] = await resolve_connector_credentials(
            session, account=row, cipher=self._cipher
        )
        knowledge = self._knowledge_factory(workspace_id)
        ctx = SkillContext(
            llm=_NoLlm(),
            config=dict(row.delivery_config or {}),
            logger=logger,
            credentials=credentials,
            knowledge=knowledge,
        )
        result = await self._runner.dispatch_action(
            meta,
            action_name=action_name,
            context=ctx,
            kwargs={},
        )
        return result if isinstance(result, dict) else {"result": result}


class _NoLlm:
    """A no-op LLM for the import :class:`SkillContext`.

    The bulk import actions read a vault / parse an export / fetch pages —
    they do not (and must not) re-enter the LLM. :class:`SkillContext`
    requires a non-None ``llm``; calling it is a bug, so it raises.
    Mirrors :class:`backend.workflow.infrastructure.connector_actions._NoLlm`.
    """

    async def chat(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("connector import must not call the LLM")


# Count keys the import actions return — we surface a single
# ``imported_count`` so the PWA "Import now" button has a uniform stat
# without per-connector branching. Falls back to summing both when the
# action splits scanned vs notes (obsidian) — the founder cares about
# successful seeds, which is the first key.
_IMPORTED_COUNT_KEYS = (
    "notes_count",  # obsidian
    "conversations_count",  # claude / gpt
    "pages_count",  # notion
    "imported_count",  # generic fallback
)


def _resolve_imported_count(detail: dict[str, Any]) -> int:
    for key in _IMPORTED_COUNT_KEYS:
        value = detail.get(key)
        if isinstance(value, int):
            return value
    return 0


async def get_import_dispatcher() -> ImportDispatcher:  # pragma: no cover — overridden in tests
    """Production :class:`ImportDispatcher` dependency.

    Loads the plugin registry (same path the delivery worker uses) + builds
    an :class:`ImportDispatcher` over a workspace-scoped
    :class:`KnowledgeFactory` constructor and the settings-derived
    :class:`CredentialCipher`. Tests override this with an in-test stub so
    a unit run never touches the loader / vault / KMS.
    """
    from backend.config import get_settings  # noqa: PLC0415
    from backend.extensions.plugin.loader import PluginLoader  # noqa: PLC0415
    from backend.knowledge.factory import KnowledgeFactory  # noqa: PLC0415
    from backend.router.accounts.crypto import _key_from_settings  # noqa: PLC0415

    # Lift R1 (v8 §D38) — connector plugins live at repo-root ``plugin/`` —
    # walk up from this module to find it. Path resolution is one-time per
    # request scope and cheap. Module path is
    # ``backend/api/v1/connectors.py`` → parents[3] is repo root.
    plugin_dir = Path(__file__).resolve().parents[3] / "plugin"  # noqa: ASYNC240
    loader = PluginLoader(plugin_dir)
    registry = await loader.load_all()
    settings = get_settings()
    vault_root = Path(settings.knowledge_vault_root)
    region = settings.knowledge_default_region

    def _knowledge(workspace_id: uuid.UUID) -> Any:
        return KnowledgeFactory(
            region=region,
            workspace_id=str(workspace_id),
            vault_root=vault_root,
        ).restricted_garden()

    return ImportDispatcher(
        plugins_by_name=dict(registry),
        cipher=CredentialCipher(_key_from_settings()),
        knowledge_factory=_knowledge,
    )


@router.post("/{connector_id}/import")
async def trigger_import(
    connector_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    dispatcher: Annotated[ImportDispatcher, Depends(get_import_dispatcher)],
) -> ConnectorImportResult:
    """Trigger an inbound bulk import for an importable connector.

    Resolves the binding (workspace-scoped) → looks up its plugin's
    import-trigger ``@p.action`` via the catalog's ``import_action`` →
    dispatches through :class:`PluginRunner` with the binding's
    ``delivery_config`` injected into the action's
    :class:`SkillContext.config`. Synchronous v1: the import runs to
    completion within the request and the response carries the count +
    timestamp. Async / streamed import is a follow-up (deferred per the
    lift's "out of scope").

    Failure modes:

    * 404 — connector not found in this workspace
    * 422 — connector is not importable: either outbound-only OR its inbound
      path is push-only / webhook-driven (e.g. ``slack``) so there is no
      bulk-import action
    * 502 — the plugin import action raised (PluginRunError)

    Emits ``audit.connector.import_triggered`` + ``…import_completed``
    (or ``…import_failed``) on the structlog stream so audit relays can
    pick the events up off the same channel they already consume.
    """
    row = await session.get(ConnectorAccountRow, connector_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"connector {connector_id} not found",
        )
    if not row.is_active:
        # A revoked binding is not an inbound source any more — same 404
        # the ingress returns so the surfaces stay aligned.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"connector {connector_id} not found",
        )
    info = _capabilities(row.connector)
    if info is None or not info.importable:
        # Not importable — no bulk-import action. (Outbound delivery and
        # webhook ingress are separate capabilities; the retired kind enum's
        # "outbound-only" vs "push-only" split was hand-assigned and is not
        # derivable from the capability flags — slack / github / telegram are
        # all outbound + webhook_trigger but not importable.)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"connector {row.connector!r} has no bulk-import action available",
        )

    logger.info(
        _AUDIT_IMPORT_TRIGGERED,
        connector_id=str(connector_id),
        connector=row.connector,
        workspace_id=str(workspace_id),
    )
    try:
        detail = await dispatcher.import_for(row=row, workspace_id=workspace_id, session=session)
    except PluginRunError as exc:
        logger.warning(
            _AUDIT_IMPORT_FAILED,
            connector_id=str(connector_id),
            connector=row.connector,
            workspace_id=str(workspace_id),
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"import failed: {exc}",
        ) from exc

    imported_count = _resolve_imported_count(detail)
    now = datetime.now(tz=UTC)
    # Persist the telemetry on the binding so the list response reflects
    # the new "last imported at / count" without a separate read path.
    row.last_import_at = now
    row.last_import_count = imported_count
    await session.commit()

    logger.info(
        _AUDIT_IMPORT_COMPLETED,
        connector_id=str(connector_id),
        connector=row.connector,
        workspace_id=str(workspace_id),
        imported_count=imported_count,
    )
    return ConnectorImportResult(
        imported_count=imported_count,
        last_import_at=now,
        detail=detail,
    )


__all__ = [
    "CatalogEntry",
    "ConnectorCatalog",
    "ConnectorCreate",
    "ConnectorCreated",
    "ConnectorImportResult",
    "ConnectorOut",
    "ImportDispatcher",
    "create_connector",
    "get_catalog",
    "get_import_dispatcher",
    "list_connectors",
    "revoke_connector",
    "router",
    "trigger_import",
]
