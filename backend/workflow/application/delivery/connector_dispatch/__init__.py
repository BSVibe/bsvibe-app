"""Connector-bound outbound delivery — close the verified-Deliverable loop.

Workflow §11.1 / §12.5 #8 (Bundle G — Delivery). A verified run mints a
:class:`~backend.workflow.infrastructure.db.Deliverable` and the orchestrator writes a
:class:`~backend.workflow.infrastructure.delivery.db.DeliveryEventRow`; the
:class:`~backend.workflow.infrastructure.workers.delivery_worker.DeliveryWorker` drains it. Until now
the drain dispatched over every plugin filtered only by the deliverable's own
``artifact_type`` (``code``), with no event payload and no credentials — so a
verified deliverable was never actually delivered OUT through a connector.

This package supplies the missing two halves:

1. **Delivery target binding = connector_account config.** A workspace's
   active :class:`~backend.connectors.db.ConnectorAccountRow` rows that carry a
   non-empty ``delivery_config`` AND whose ``connector`` exposes an
   ``@p.outbound`` are the delivery targets. The routing / system fields
   (e.g. notion's ``parent_page_id``) come from this STABLE founder-set config
   — never from LLM / work output (no-LLM-output-for-system-fields rule).

2. **Per-connector event shaping.** :data:`OUTBOUND_EVENT_BUILDERS` maps a
   connector name to a builder that turns ``{deliverable content} +
   {delivery_config}`` into the bespoke event keys the connector's outbound
   expects, plus the ``artifact_type`` to dispatch under. v1 ships notion +
   slack + email-sender + telegram + discord + linear + trello (the
   pattern-setter — no git-ops, unlike github PRs); github is the special case
   that needs git-ops (see :mod:`._github`); sentry follows when its mapper
   lands — a connector with no builder is a deliberate seam (skipped, no
   error).

:class:`ConnectorDeliveryAdapter` implements the worker's
:class:`~backend.workflow.infrastructure.workers.delivery_worker.PluginDispatchAdapter` Protocol: it
loads the Deliverable, resolves the binding(s), shapes the event from config +
content, and dispatches THAT connector's outbound through the existing
:class:`~backend.workflow.application.delivery.dispatcher.DeliveryDispatcher` /
:class:`~backend.extensions.plugin.runner.PluginRunner`. Resolving zero bindings is a
no-op success — the in-app Deliverable still exists, nothing is delivered out,
no error (the event still drains so the queue never wedges).

**Package layout (Lift §17.7 partial decomposition).**

* :mod:`._builders` — :class:`ShapedEvent`, the 7 per-connector event builders,
  the :data:`OUTBOUND_EVENT_BUILDERS` map.
* :mod:`._resolver` — workspace → bindings (the simple-builder bindings AND
  the github special-case binding).
* :mod:`._github` — github clone provisioner + commit/push/PR delivery
  handler (the one connector that needs git-ops, not a simple event dict).
* :mod:`._context` — :class:`SkillContext` + no-op LLM shared by the adapter
  and github handler.
* this module — the worker-facing :class:`ConnectorDeliveryAdapter` (the
  cohesive entry) + :func:`build_connector_delivery_adapter` factory + the
  re-exports every caller (REST API / worker bootstrap / tests) imports.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.extensions.plugin.base import PluginMeta
from backend.extensions.plugin.runner import PluginRunner
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.application.delivery.dispatcher import DeliveryDispatcher
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import Deliverable
from backend.workflow.infrastructure.delivery.git_ops import GitOps

from ._builders import (
    OUTBOUND_EVENT_BUILDERS,
    OutboundEventBuilder,
    ShapedEvent,
    _split_summary,
    _summary_with_refs,
    build_discord_event,
    build_email_event,
    build_linear_event,
    build_notion_event,
    build_slack_event,
    build_telegram_event,
    build_trello_event,
)
from ._context import _build_context, _NoLlm
from ._github import (
    GithubDeliveryDeps,
    build_github_workspace_provisioner,
    deliver_github,
    github_remote_url,
    run_branch_name,
)
from ._resolver import (
    GithubBinding,
    _Binding,
    _resolve_bindings,
    resolve_github_binding,
)

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class ConnectorDeliveryAdapter:
    """Resolve the connector binding(s), shape the event, dispatch the outbound.

    Implements :class:`~backend.workflow.infrastructure.workers.delivery_worker.PluginDispatchAdapter`.
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
    #: Where each run's workspace checkout lives (``workspace_root/<run_id>``).
    #: Required for the github special case (commit/push against the clone the
    #: run-setup provisioner made). ``None`` disables github delivery (the other
    #: connectors are unaffected — they need no checkout).
    workspace_root: Path | None = None
    #: Git CLI wrapper for the github commit→push (token-scrubbed). Injectable so
    #: tests can point it at a stub if needed; defaults to the real ``git``.
    git_ops: GitOps = field(default_factory=GitOps)
    #: ``owner/name -> clone/push URL``. Defaults to github.com HTTPS; tests
    #: override this to a LOCAL bare repo so the push lands without network.
    remote_url_for: Callable[[str], str] = github_remote_url
    #: Runner for the github ``open_pr`` action (the other connectors dispatch
    #: their outbound through ``self.dispatcher``; the github special case calls
    #: an *action* directly, so it needs its own runner).
    runner: PluginRunner = field(default_factory=PluginRunner)

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
            run_id = deliverable.run_id if deliverable is not None else None
            bindings = await _resolve_bindings(
                session,
                workspace_id=workspace_id,
                plugins_by_name=self.plugins_by_name,
            )
            github_binding = await resolve_github_binding(session, workspace_id=workspace_id)

        actions: list[ActionResult] = []
        if github_binding is not None:
            actions.extend(
                await deliver_github(
                    deps=GithubDeliveryDeps(
                        cipher=self.cipher,
                        plugins_by_name=self.plugins_by_name,
                        workspace_root=self.workspace_root,
                        git_ops=self.git_ops,
                        remote_url_for=self.remote_url_for,
                        runner=self.runner,
                        session_factory=self.session_factory,
                    ),
                    binding=github_binding,
                    workspace_id=workspace_id,
                    deliverable_id=deliverable_id,
                    run_id=run_id,
                    content=content,
                )
            )
        for binding in bindings:
            try:
                shaped = binding.builder(content, dict(binding.account.delivery_config))
            except ValueError as exc:
                # A builder raises ValueError for a misconfigured delivery
                # target (e.g. slack with no ``channel``, email with no ``to``)
                # — mirrors notion raising on a missing ``parent_page_id``. Soft
                # -fail it into a failed action (like a per-plugin dispatch
                # failure) so a single bad target does not wedge the queue.
                logger.warning(
                    "connector_delivery_build_failed",
                    connector=binding.account.connector,
                    workspace_id=str(workspace_id),
                    deliverable_id=str(deliverable_id),
                    error=str(exc),
                )
                actions.append(
                    ActionResult(
                        action=f"{binding.account.connector}:outbound:build",
                        succeeded=False,
                        error=str(exc),
                    )
                )
                continue
            credentials: dict[str, Any] = {
                shaped.credential_key: self.cipher.decrypt(
                    binding.account.signing_secret_ciphertext
                )
            }
            # Connectors needing a second (non-secret) credential slot — e.g.
            # trello's app-level ``api_key`` alongside the secret ``token`` —
            # carry it in ``extra_credentials`` (sourced from the founder-set
            # delivery_config), since a connector_account stores only one secret.
            credentials.update(shaped.extra_credentials)
            ctx = _build_context(
                credentials=credentials,
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

    async def _deliver_github(
        self,
        *,
        binding: GithubBinding,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
        run_id: uuid.UUID | None,
        content: dict[str, Any],
    ) -> list[ActionResult]:
        """Back-compat shim — delegates to :func:`_github.deliver_github`.

        Kept on the adapter so existing defensive-branch tests that exercise the
        github special case directly (without going through the full
        ``dispatch`` path) still work post-Lift §17.7. New callers should use
        :func:`._github.deliver_github` with an explicit
        :class:`GithubDeliveryDeps`.
        """
        return await deliver_github(
            deps=GithubDeliveryDeps(
                cipher=self.cipher,
                plugins_by_name=self.plugins_by_name,
                workspace_root=self.workspace_root,
                git_ops=self.git_ops,
                remote_url_for=self.remote_url_for,
                runner=self.runner,
                session_factory=self.session_factory,
            ),
            binding=binding,
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            run_id=run_id,
            content=content,
        )


def build_connector_delivery_adapter(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    plugins: list[PluginMeta],
    cipher: CredentialCipher,
    dispatcher: DeliveryDispatcher | None = None,
    workspace_root: Path | None = None,
    git_ops: GitOps | None = None,
    remote_url_for: Callable[[str], str] | None = None,
) -> ConnectorDeliveryAdapter:
    """Wrap loaded plugins + a cipher into a worker-facing delivery adapter.

    ``workspace_root`` enables the github special case (commit/push the run's
    checkout under ``workspace_root/<run_id>`` + open a PR). ``remote_url_for``
    overrides the clone/push URL (tests point it at a LOCAL bare repo); it
    defaults to github.com HTTPS. The other connectors need none of these.
    """
    return ConnectorDeliveryAdapter(
        session_factory=session_factory,
        plugins_by_name={p.name: p for p in plugins},
        cipher=cipher,
        dispatcher=dispatcher or DeliveryDispatcher(runner=PluginRunner()),
        workspace_root=workspace_root,
        git_ops=git_ops or GitOps(),
        remote_url_for=remote_url_for or github_remote_url,
        runner=PluginRunner(),
    )


__all__ = [
    "OUTBOUND_EVENT_BUILDERS",
    "ConnectorDeliveryAdapter",
    "GithubBinding",
    "OutboundEventBuilder",
    "ShapedEvent",
    "_Binding",
    "_NoLlm",
    "_build_context",
    "_resolve_bindings",
    "_split_summary",
    "_summary_with_refs",
    "build_connector_delivery_adapter",
    "build_discord_event",
    "build_email_event",
    "build_github_workspace_provisioner",
    "build_linear_event",
    "build_notion_event",
    "build_slack_event",
    "build_telegram_event",
    "build_trello_event",
    "deliver_github",
    "github_remote_url",
    "resolve_github_binding",
    "run_branch_name",
]
