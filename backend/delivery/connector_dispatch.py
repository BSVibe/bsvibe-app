"""Connector-bound outbound delivery — close the verified-Deliverable loop.

Workflow §11.1 / §12.5 #8 (Bundle G — Delivery). A verified run mints a
:class:`~backend.execution.db.Deliverable` and the orchestrator writes a
:class:`~backend.delivery.db.DeliveryEventRow`; the
:class:`~backend.workers.delivery_worker.DeliveryWorker` drains it. Until now
the drain dispatched over every plugin filtered only by the deliverable's own
``artifact_type`` (``code``), with no event payload and no credentials — so a
verified deliverable was never actually delivered OUT through a connector.

This module supplies the missing two halves:

1. **Delivery target binding = connector_account config.** A workspace's
   active :class:`~backend.connectors.db.ConnectorAccountRow` rows that carry a
   non-empty ``delivery_config`` AND whose ``connector`` exposes an
   ``@p.outbound`` are the delivery targets. The routing / system fields
   (e.g. notion's ``parent_page_id``) come from this STABLE founder-set config
   — never from LLM / work output (no-LLM-output-for-system-fields rule).

2. **Per-connector event shaping.** :data:`OUTBOUND_EVENT_BUILDERS` maps a
   connector name to a builder that turns ``{deliverable content} +
   {delivery_config}`` into the bespoke event keys the connector's outbound
   expects, plus the ``artifact_type`` to dispatch under. v1 ships only
   ``notion`` (the pattern-setter — no git-ops, unlike github PRs); the other
   eight connectors are a deliberate seam: no builder → skipped, no error.

:class:`ConnectorDeliveryAdapter` implements the worker's
:class:`~backend.workers.delivery_worker.PluginDispatchAdapter` Protocol: it
loads the Deliverable, resolves the binding(s), shapes the event from config +
content, and dispatches THAT connector's outbound through the existing
:class:`~backend.delivery.dispatcher.DeliveryDispatcher` /
:class:`~backend.plugins.runner.PluginRunner`. Resolving zero bindings is a
no-op success — the in-app Deliverable still exists, nothing is delivered out,
no error (the event still drains so the queue never wedges).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.accounts.crypto import CredentialCipher
from backend.connectors.db import ConnectorAccountRow
from backend.delivery.dispatcher import DeliveryDispatcher
from backend.delivery.schema import ActionResult, ArtifactType, DeliveryResult
from backend.execution.db import Deliverable
from backend.plugins.base import PluginMeta
from backend.plugins.context import SkillContext
from backend.plugins.runner import PluginRunner

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Event shaping — one builder per connector (notion is the v1 pattern-setter)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ShapedEvent:
    """The dispatch-ready outbound: which ``artifact_type`` + the event dict."""

    artifact_type: ArtifactType
    event: dict[str, Any]


# A builder maps {deliverable content} + {connector delivery_config} → a
# ShapedEvent. Content (title/body) is sourced from the deliverable; routing /
# system fields (e.g. parent_page_id) from the stable config.
OutboundEventBuilder = Callable[[dict[str, Any], dict[str, Any]], ShapedEvent]


def _split_summary(summary: str) -> tuple[str, str]:
    """First non-empty line → title; the full summary → body.

    A deliverable summary is free-form text. The first line is the most
    title-like fragment; the whole summary is kept as the body so no content is
    dropped. Empty summary → a stable placeholder title (Notion rejects an
    empty title property).
    """
    lines = [ln.strip() for ln in summary.splitlines() if ln.strip()]
    title = lines[0] if lines else "Delivered artifact"
    return title, summary.strip()


def build_notion_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into notion's ``deliver_page`` event.

    * ``parent_page_id`` — routing, from the stable ``delivery_config``.
    * ``title`` — first line of the deliverable summary.
    * ``body`` — the deliverable summary, with any ``artifact_refs`` appended
      as a trailing reference list (so the delivered page links the produced
      artifacts).
    """
    summary = str(content.get("summary") or "")
    title, body = _split_summary(summary)
    artifact_refs = content.get("artifact_refs") or []
    if artifact_refs:
        refs = "\n".join(f"- {ref}" for ref in artifact_refs)
        body = f"{body}\n\nArtifacts:\n{refs}" if body else f"Artifacts:\n{refs}"
    return ShapedEvent(
        artifact_type="page",
        event={
            "parent_page_id": delivery_config["parent_page_id"],
            "title": title,
            "body": body,
        },
    )


# The extensible seam: a connector with no entry here has no v1 outbound
# event-shaping and is skipped (logged) — github/slack/telegram/discord/email/
# linear/sentry/trello follow the SAME (content, config) -> ShapedEvent shape
# when their mappers land. DO NOT implement them in this chunk.
OUTBOUND_EVENT_BUILDERS: dict[str, OutboundEventBuilder] = {
    "notion": build_notion_event,
}


# ---------------------------------------------------------------------------
# Binding resolution
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Binding:
    account: ConnectorAccountRow
    plugin: PluginMeta
    builder: OutboundEventBuilder


async def _resolve_bindings(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    plugins_by_name: dict[str, PluginMeta],
) -> list[_Binding]:
    """Active connector_accounts for the workspace that are deliverable targets.

    A row qualifies when ALL hold: it is ``is_active``, its ``delivery_config``
    is non-empty, its ``connector`` has a loaded plugin that declares at least
    one ``@p.outbound``, AND a v1 event-builder exists for that connector. Rows
    failing any condition are skipped (the others without a builder are the
    deliberate seam for connectors not yet wired).
    """
    rows = (
        (
            await session.execute(
                select(ConnectorAccountRow).where(
                    ConnectorAccountRow.workspace_id == workspace_id,
                    ConnectorAccountRow.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    bindings: list[_Binding] = []
    for row in rows:
        if not row.delivery_config:
            continue
        plugin = plugins_by_name.get(row.connector)
        if plugin is None or not plugin.outbounds:
            continue
        builder = OUTBOUND_EVENT_BUILDERS.get(row.connector)
        if builder is None:
            logger.info(
                "connector_delivery_no_builder_skipped",
                connector=row.connector,
                workspace_id=str(workspace_id),
            )
            continue
        bindings.append(_Binding(account=row, plugin=plugin, builder=builder))
    return bindings


# ---------------------------------------------------------------------------
# SkillContext for an outbound call
# ---------------------------------------------------------------------------


class _NoLlm:
    """A no-op LLM for the outbound SkillContext.

    Connector outbound functions only read ``context.credentials`` /
    ``context.config`` (the delivery is a single REST call, not an agent loop),
    but :class:`SkillContext` requires a non-None ``llm``. Calling it is a bug,
    so it raises rather than silently no-opping.
    """

    async def chat(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("connector outbound delivery must not call the LLM")


def _build_context(*, credentials: dict[str, Any], config: dict[str, Any]) -> SkillContext:
    return SkillContext(
        llm=_NoLlm(),
        config=config,
        logger=logger,
        credentials=credentials,
    )


# ---------------------------------------------------------------------------
# The worker-facing adapter
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ConnectorDeliveryAdapter:
    """Resolve the connector binding(s), shape the event, dispatch the outbound.

    Implements :class:`~backend.workers.delivery_worker.PluginDispatchAdapter`.
    The worker hands ``(workspace_id, deliverable_id, artifact_type)``; this
    adapter loads the Deliverable's content, resolves the workspace's delivery
    bindings, and for each shapes + dispatches the connector outbound through
    the real :class:`DeliveryDispatcher`. Per-binding failures aggregate into
    the returned :class:`DeliveryResult.actions` (a real failed delivery
    surfaces as ``succeeded=False``); resolving NO binding is an empty,
    successful result (no external delivery, no error).
    """

    session_factory: async_sessionmaker[AsyncSession]
    plugins_by_name: dict[str, PluginMeta]
    cipher: CredentialCipher
    dispatcher: DeliveryDispatcher = field(default_factory=DeliveryDispatcher)

    async def dispatch(
        self,
        *,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
        artifact_type: str,
        plugins: Any = (),
        context: Any = None,
        event: Any = None,
    ) -> DeliveryResult:
        async with self.session_factory() as session:
            deliverable = await session.get(Deliverable, deliverable_id)
            content = dict(deliverable.payload) if deliverable is not None else {}
            bindings = await _resolve_bindings(
                session,
                workspace_id=workspace_id,
                plugins_by_name=self.plugins_by_name,
            )

        actions: list[ActionResult] = []
        for binding in bindings:
            shaped = binding.builder(content, dict(binding.account.delivery_config))
            ctx = _build_context(
                credentials={
                    "token": self.cipher.decrypt(binding.account.signing_secret_ciphertext)
                },
                config=dict(binding.account.delivery_config),
            )
            result = await self.dispatcher.dispatch(
                workspace_id=workspace_id,
                deliverable_id=deliverable_id,
                artifact_type=shaped.artifact_type,
                plugins=[binding.plugin],
                context=ctx,
                event=shaped.event,
            )
            actions.extend(result.actions)
            logger.info(
                "connector_delivery_dispatched",
                connector=binding.account.connector,
                workspace_id=str(workspace_id),
                deliverable_id=str(deliverable_id),
                actions=len(result.actions),
            )

        # The persisted DeliveryResult uses the deliverable's own artifact_type
        # (e.g. "code") — it identifies the deliverable, not the per-connector
        # artifact_type dispatched (e.g. "page").
        return DeliveryResult(
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,  # type: ignore[arg-type]  # validated at the schema layer
            actions=actions,
            error=None if any(a.succeeded for a in actions) or not actions else actions[-1].error,
        )


def build_connector_delivery_adapter(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    plugins: list[PluginMeta],
    cipher: CredentialCipher,
    dispatcher: DeliveryDispatcher | None = None,
) -> ConnectorDeliveryAdapter:
    """Wrap loaded plugins + a cipher into a worker-facing delivery adapter."""
    return ConnectorDeliveryAdapter(
        session_factory=session_factory,
        plugins_by_name={p.name: p for p in plugins},
        cipher=cipher,
        dispatcher=dispatcher or DeliveryDispatcher(runner=PluginRunner()),
    )


__all__ = [
    "OUTBOUND_EVENT_BUILDERS",
    "ConnectorDeliveryAdapter",
    "OutboundEventBuilder",
    "ShapedEvent",
    "build_connector_delivery_adapter",
    "build_notion_event",
]
