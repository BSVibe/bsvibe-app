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
:class:`~backend.extensions.plugin.runner.PluginRunner`. Resolving zero bindings is a
no-op success — the in-app Deliverable still exists, nothing is delivered out,
no error (the event still drains so the queue never wedges).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.delivery.dispatcher import DeliveryDispatcher
from backend.delivery.git_ops import GitOps
from backend.delivery.schema import ActionResult, ArtifactType, DeliveryResult
from backend.execution.db import Deliverable
from backend.extensions.plugin.base import PluginMeta
from backend.extensions.plugin.context import SkillContext
from backend.extensions.plugin.runner import PluginRunner
from backend.router.accounts.crypto import CredentialCipher

logger = structlog.get_logger(__name__)


def github_remote_url(repo: str) -> str:
    """Default github HTTPS clone/push URL for an ``owner/name`` repo."""
    return f"https://github.com/{repo}.git"


def run_branch_name(run_id: uuid.UUID) -> str:
    """The per-run delivery branch — ``bsvibe/run-<short id>`` (Workflow §3.1).

    Short id (first 8 hex chars of the run UUID) keeps the branch name readable
    while staying unique per run. The branch is created at clone time (run
    setup) and is what the PR is opened from.
    """
    return f"bsvibe/run-{run_id.hex[:8]}"


# ---------------------------------------------------------------------------
# Event shaping — one builder per connector (notion is the v1 pattern-setter)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ShapedEvent:
    """The dispatch-ready outbound: which ``artifact_type`` + the event dict.

    ``credential_key`` is the credential slot the decrypted per-account secret
    is injected under for THIS connector's outbound. Connectors read their
    token from different keys (notion ``token``, slack ``bot_token``,
    email-sender ``api_key``); the builder declares which one so the adapter
    lands the single stored secret in the slot the plugin's ``_client`` reads.

    ``extra_credentials`` carries ADDITIONAL non-secret credential slots a
    connector needs alongside the single decrypted account secret. A
    ``connector_account`` stores exactly one encrypted secret
    (``signing_secret_ciphertext``), but a few connectors authenticate with two
    values — e.g. trello sends BOTH a ``key`` (its API key, an app-level public
    identifier) and a ``token`` (the user-authorizing secret) as query params.
    The genuinely secret half (trello ``token``) is the decrypted account secret
    under ``credential_key``; the non-secret half (trello ``api_key``) is sourced
    from the founder-set ``delivery_config`` and carried here so the adapter can
    inject both into ``context.credentials``. This avoids changing the
    single-secret ``connector_account`` schema. See :func:`build_trello_event`.
    """

    artifact_type: ArtifactType
    event: dict[str, Any]
    credential_key: str = "token"
    extra_credentials: dict[str, str] = field(default_factory=dict)


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


def build_slack_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into slack's ``deliver_message`` event.

    * ``channel`` — routing, from the stable ``delivery_config`` (never derived
      from the work text). A missing / empty ``channel`` is a misconfigured
      delivery target → ``ValueError`` (mirrors notion raising on a missing
      ``parent_page_id``), surfaced as a failed action rather than posting to a
      wrong / default channel.
    * ``text`` — the deliverable summary, with any ``artifact_refs`` appended as
      a trailing reference list so the message links the produced artifacts.

    ``artifact_type`` is ``slack_message`` (what slack's ``@p.outbound``
    declares); the decrypted account secret is injected as ``bot_token``.
    """
    channel = delivery_config.get("channel")
    if not channel:
        raise ValueError("slack delivery_config missing required 'channel'")
    summary = str(content.get("summary") or "")
    _title, text = _split_summary(summary)
    artifact_refs = content.get("artifact_refs") or []
    if artifact_refs:
        refs = "\n".join(f"- {ref}" for ref in artifact_refs)
        text = f"{text}\n\nArtifacts:\n{refs}" if text else f"Artifacts:\n{refs}"
    return ShapedEvent(
        artifact_type="slack_message",
        event={"channel": str(channel), "text": text},
        credential_key="bot_token",
    )


def build_email_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into email-sender's ``deliver_email`` event.

    * ``to`` — routing, from the stable ``delivery_config`` (never derived from
      the work text). A missing / empty ``to`` is a misconfigured delivery
      target → ``ValueError`` (mirrors notion raising on a missing
      ``parent_page_id``).
    * ``from`` — optional founder-set sender override from ``delivery_config``;
      omitted when unset so the email-sender plugin falls back to its own
      ``email_from`` config / ``from`` credential.
    * ``subject`` — first non-empty line of the deliverable summary.
    * ``body`` — the deliverable summary (sent as plain text via ``as_text``),
      with any ``artifact_refs`` appended as a trailing reference list.

    ``artifact_type`` is ``email`` (what email-sender's ``@p.outbound``
    declares); the decrypted account secret is injected as ``api_key``.
    """
    to = delivery_config.get("to")
    if not to:
        raise ValueError("email delivery_config missing required 'to'")
    summary = str(content.get("summary") or "")
    subject, body = _split_summary(summary)
    artifact_refs = content.get("artifact_refs") or []
    if artifact_refs:
        refs = "\n".join(f"- {ref}" for ref in artifact_refs)
        body = f"{body}\n\nArtifacts:\n{refs}" if body else f"Artifacts:\n{refs}"
    event: dict[str, Any] = {
        "to": str(to),
        "subject": subject,
        "body": body,
        "as_text": True,
    }
    sender = delivery_config.get("from")
    if sender:
        event["from"] = str(sender)
    return ShapedEvent(
        artifact_type="email",
        event=event,
        credential_key="api_key",
    )


def _summary_with_refs(content: dict[str, Any]) -> tuple[str, str]:
    """``(title, body)`` from the deliverable summary, with ``artifact_refs``
    appended to the body as a trailing reference list.

    Shared by the message-style builders (telegram/discord) and the
    issue/card-style builders (linear/trello): the title is the first non-empty
    summary line, the body is the whole summary plus a linked artifact list so no
    produced artifact is dropped from the delivered content.
    """
    summary = str(content.get("summary") or "")
    title, body = _split_summary(summary)
    artifact_refs = content.get("artifact_refs") or []
    if artifact_refs:
        refs = "\n".join(f"- {ref}" for ref in artifact_refs)
        body = f"{body}\n\nArtifacts:\n{refs}" if body else f"Artifacts:\n{refs}"
    return title, body


def build_telegram_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into telegram's ``deliver_message`` event.

    * ``chat_id`` — routing, from the stable ``delivery_config`` (never derived
      from the work text). A missing / empty ``chat_id`` is a misconfigured
      delivery target → ``ValueError`` (mirrors notion raising on a missing
      ``parent_page_id``).
    * ``text`` — the deliverable summary, with any ``artifact_refs`` appended as
      a trailing reference list.

    ``artifact_type`` is ``telegram_message`` (what telegram's ``@p.outbound``
    declares); the decrypted account secret is injected as ``bot_token``.
    """
    chat_id = delivery_config.get("chat_id")
    if not chat_id:
        raise ValueError("telegram delivery_config missing required 'chat_id'")
    _title, text = _summary_with_refs(content)
    return ShapedEvent(
        artifact_type="telegram_message",
        event={"chat_id": str(chat_id), "text": text},
        credential_key="bot_token",
    )


def build_discord_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into discord's ``deliver_message`` event.

    * ``channel_id`` — routing, from the stable ``delivery_config`` (never
      derived from the work text). A missing / empty ``channel_id`` is a
      misconfigured delivery target → ``ValueError`` (mirrors notion raising on
      a missing ``parent_page_id``).
    * ``content`` — the deliverable summary, with any ``artifact_refs`` appended
      as a trailing reference list.

    ``artifact_type`` is ``discord_message`` (what discord's ``@p.outbound``
    declares); the decrypted account secret is injected as ``bot_token``.
    """
    channel_id = delivery_config.get("channel_id")
    if not channel_id:
        raise ValueError("discord delivery_config missing required 'channel_id'")
    _title, body = _summary_with_refs(content)
    return ShapedEvent(
        artifact_type="discord_message",
        event={"channel_id": str(channel_id), "content": body},
        credential_key="bot_token",
    )


def build_linear_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into linear's ``deliver_issue`` event.

    * ``team_id`` — routing, from the stable ``delivery_config`` (never derived
      from the work text). A missing / empty ``team_id`` is a misconfigured
      delivery target → ``ValueError`` (mirrors notion raising on a missing
      ``parent_page_id``). NOTE: the linear plugin also falls back to
      ``config['linear_team_id']``, but the builder explicitly sets the event
      ``team_id`` so the routing source is unambiguous and config-driven.
    * ``title`` — first non-empty line of the deliverable summary.
    * ``description`` — the deliverable summary, with any ``artifact_refs``
      appended as a trailing reference list.

    ``artifact_type`` is ``issue`` (what linear's ``@p.outbound`` declares); the
    decrypted account secret is injected as ``api_key``.
    """
    team_id = delivery_config.get("team_id")
    if not team_id:
        raise ValueError("linear delivery_config missing required 'team_id'")
    title, description = _summary_with_refs(content)
    return ShapedEvent(
        artifact_type="issue",
        event={"team_id": str(team_id), "title": title, "description": description},
        credential_key="api_key",
    )


def build_trello_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into trello's ``deliver_card`` event.

    * ``list_id`` — routing, from the stable ``delivery_config`` (never derived
      from the work text). A missing / empty ``list_id`` is a misconfigured
      delivery target → ``ValueError`` (mirrors notion raising on a missing
      ``parent_page_id``).
    * ``title`` — first non-empty line of the deliverable summary (the trello
      plugin maps the event ``title`` to the card ``name``).
    * ``desc`` — the deliverable summary, with any ``artifact_refs`` appended as
      a trailing reference list.

    **Dual-secret caveat.** Trello authenticates with TWO query-param values:
    a ``key`` (its API key — an app-level, non-user-secret identifier) and a
    ``token`` (the user-authorizing secret). A ``connector_account`` stores only
    ONE encrypted secret (``signing_secret_ciphertext``) — we use it for the
    genuinely secret half, the trello ``token`` (``credential_key="token"``).
    The non-secret ``api_key`` is sourced from the founder-set
    ``delivery_config['api_key']`` and carried in ``extra_credentials`` so the
    adapter injects both slots the trello ``_client`` reads — WITHOUT changing
    the single-secret ``connector_account`` schema. A missing config ``api_key``
    is a misconfigured target → ``ValueError`` (the trello client requires both).

    If trello ever needs the API key kept secret too, the proper fix is a richer
    multi-secret ``connector_account`` credential model (out of scope here).
    """
    list_id = delivery_config.get("list_id")
    if not list_id:
        raise ValueError("trello delivery_config missing required 'list_id'")
    api_key = delivery_config.get("api_key")
    if not api_key:
        raise ValueError("trello delivery_config missing required 'api_key'")
    title, desc = _summary_with_refs(content)
    return ShapedEvent(
        artifact_type="card",
        event={"list_id": str(list_id), "title": title, "desc": desc},
        credential_key="token",
        extra_credentials={"api_key": str(api_key)},
    )


# The extensible seam: a connector with no entry here has no v1 outbound
# event-shaping and is skipped (logged). This ships notion + slack +
# email-sender + telegram + discord + linear + trello; github (needs a git-ops
# layer) and sentry follow the SAME (content, config) -> ShapedEvent shape when
# their mappers land. Keys MUST match the plugin ``name=`` (and the
# ``connector_accounts.connector`` value) so binding resolution lines up — note
# the email connector's name is ``email-sender``, not ``email``.
OUTBOUND_EVENT_BUILDERS: dict[str, OutboundEventBuilder] = {
    "notion": build_notion_event,
    "slack": build_slack_event,
    "email-sender": build_email_event,
    "telegram": build_telegram_event,
    "discord": build_discord_event,
    "linear": build_linear_event,
    "trello": build_trello_event,
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
# github binding resolution (shared by run-setup clone + delivery handler)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GithubBinding:
    """A workspace's github delivery target: the account + its ``repo`` config.

    ``repo`` is the founder-set ``delivery_config['repo']`` (``owner/name``);
    ``base_branch`` is ``delivery_config['base_branch']`` (default ``main``). The
    github connector's encrypted secret IS the git push / API token (the same
    secret slot the inbound webhook uses — connectors reuse the one stored
    secret).
    """

    account: ConnectorAccountRow
    repo: str
    base_branch: str


async def resolve_github_binding(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> GithubBinding | None:
    """The workspace's active github delivery target, or ``None``.

    Mirrors :func:`_resolve_bindings` but for the github special case (github is
    NOT a simple event builder — it needs git-ops, not just an event dict). A
    row qualifies when it is ``is_active``, its ``connector`` is ``github``, and
    its ``delivery_config`` carries a non-empty ``repo``. The first such row
    wins (a workspace has one github delivery target in v1).
    """
    rows = (
        (
            await session.execute(
                select(ConnectorAccountRow).where(
                    ConnectorAccountRow.workspace_id == workspace_id,
                    ConnectorAccountRow.connector == "github",
                    ConnectorAccountRow.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        repo = (row.delivery_config or {}).get("repo")
        if not repo:
            continue
        base_branch = str((row.delivery_config or {}).get("base_branch") or "main")
        return GithubBinding(account=row, repo=str(repo), base_branch=base_branch)
    return None


# ---------------------------------------------------------------------------
# Run-setup workspace provisioner (clone the github target into the run dir)
# ---------------------------------------------------------------------------


def build_github_workspace_provisioner(
    *,
    cipher: CredentialCipher | Callable[[], CredentialCipher],
    git_ops: GitOps | None = None,
    remote_url_for: Callable[[str], str] | None = None,
) -> Callable[[AsyncSession, Any, Path], Any]:
    """A :attr:`AgentExecutionDeps.workspace_provisioner` for the github path.

    The returned coroutine resolves the run's workspace github connector binding
    and, when present, CLONES the target repo into ``workspace_dir`` on a new
    ``bsvibe/run-<short id>`` branch — so the agent's file_write/file_edit
    operate on a REAL checkout a PR diff can be built from. No github binding →
    a no-op (the empty scratch dir is used exactly as the non-github path; the
    Direct-path tests, which inject no provisioner at all, are unaffected).

    ``cipher`` may be a :class:`CredentialCipher` or a zero-arg factory returning
    one — the factory is called LAZILY only when a github binding is actually
    present, so a run with no github target never forces the KMS key (no
    credential is decrypted). The clone is token-authed with the decrypted
    github secret (never logged). ``remote_url_for`` overrides the clone URL
    (tests point it at a LOCAL bare repo); it defaults to github.com HTTPS.
    """
    ops = git_ops or GitOps()
    url_for = remote_url_for or github_remote_url

    def _resolve_cipher() -> CredentialCipher:
        return cipher() if callable(cipher) else cipher

    async def _provision(session: AsyncSession, run: Any, workspace_dir: Path) -> None:
        binding = await resolve_github_binding(session, workspace_id=run.workspace_id)
        if binding is None:
            return
        token = _resolve_cipher().decrypt(binding.account.signing_secret_ciphertext)
        # The provisioner is handed a freshly-created (empty) workspace_dir; git
        # clone refuses a non-empty target, so remove the empty dir and let
        # clone create it. (Local FS calls — the run setup is not hot-path I/O.)
        if workspace_dir.exists() and not any(workspace_dir.iterdir()):  # noqa: ASYNC240
            workspace_dir.rmdir()  # noqa: ASYNC240
        await ops.clone(url_for(binding.repo), workspace_dir, token=token, depth=1)
        await ops.checkout_new_branch(workspace_dir, run_branch_name(run.id))
        logger.info(
            "github_run_workspace_cloned",
            workspace_id=str(run.workspace_id),
            run_id=str(run.id),
            repo=binding.repo,
            branch=run_branch_name(run.id),
        )

    return _provision


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
                await self._deliver_github(
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
        """Commit the run's checkout → push the branch → open a PR.

        github is the one delivery target that needs a real DIFF, so it is a
        special case (NOT a simple event builder): the run already WORKED inside
        a clone of the target repo (the run-setup provisioner cloned it onto a
        ``bsvibe/run-<id>`` branch). Here we ``commit_all`` the agent's edits,
        ``push`` that branch, then call the github plugin's ``open_pr`` action.

        **No changes in the checkout → no PR, clean no-op success** (so a
        non-code run in a github workspace does not open an empty PR). A missing
        ``workspace_root`` / checkout dir / run id is a misconfigured target →
        soft-fails into a failed action (the queue never wedges), mirroring the
        builder ValueError path.
        """
        action_prefix = "github:outbound:pr"
        if self.workspace_root is None or run_id is None:
            logger.warning(
                "github_delivery_no_workspace_root",
                workspace_id=str(workspace_id),
                deliverable_id=str(deliverable_id),
            )
            return [
                ActionResult(
                    action=action_prefix,
                    succeeded=False,
                    error="github delivery requires a workspace_root + run id",
                )
            ]
        checkout = self.workspace_root / str(run_id)
        if not checkout.exists():
            logger.warning(
                "github_delivery_checkout_missing",
                workspace_id=str(workspace_id),
                deliverable_id=str(deliverable_id),
                run_id=str(run_id),
            )
            return [
                ActionResult(
                    action=action_prefix,
                    succeeded=False,
                    error="github delivery checkout does not exist",
                )
            ]

        token = self.cipher.decrypt(binding.account.signing_secret_ciphertext)
        branch = run_branch_name(run_id)
        summary = str(content.get("summary") or "")
        title, body = _split_summary(summary)

        # 1. Commit the agent's file edits. No changes → no PR (clean no-op).
        committed = await self.git_ops.commit_all(checkout, title)
        if not committed:
            logger.info(
                "github_delivery_no_changes_noop",
                workspace_id=str(workspace_id),
                deliverable_id=str(deliverable_id),
                run_id=str(run_id),
            )
            return [
                ActionResult(
                    action=action_prefix,
                    succeeded=True,
                    output={"skipped": True, "reason": "no_changes"},
                )
            ]

        # 2. Push the branch to the (real-or-test) remote.
        remote_url = self.remote_url_for(binding.repo)
        await self.git_ops.push(checkout, branch, token=token)

        # 3. Open the PR via the github plugin's open_pr action. Routing
        #    (repo/base) is the stable founder-set config; head is the run
        #    branch; title/body come from the deliverable summary (content).
        plugin = self.plugins_by_name.get("github")
        if plugin is None:
            return [
                ActionResult(
                    action=action_prefix,
                    succeeded=False,
                    error="github plugin not loaded",
                )
            ]
        ctx = _build_context(
            credentials={"token": token},
            config=dict(binding.account.delivery_config),
        )
        try:
            result = await self.runner.dispatch_action(
                plugin,
                action_name="open_pr",
                context=ctx,
                kwargs={
                    "repo": binding.repo,
                    "head": branch,
                    "base": binding.base_branch,
                    "title": title,
                    "body": body,
                },
            )
        except Exception as exc:  # noqa: BLE001 — soft-fail like a plugin failure
            logger.warning(
                "github_delivery_open_pr_failed",
                workspace_id=str(workspace_id),
                deliverable_id=str(deliverable_id),
                error=str(exc),
            )
            return [ActionResult(action=action_prefix, succeeded=False, error=str(exc))]

        output = dict(result) if isinstance(result, dict) else {"result": result}
        logger.info(
            "github_delivery_pr_opened",
            workspace_id=str(workspace_id),
            deliverable_id=str(deliverable_id),
            run_id=str(run_id),
            branch=branch,
            repo=binding.repo,
            remote=remote_url,
        )
        return [ActionResult(action=action_prefix, succeeded=True, output=output)]


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
    "build_connector_delivery_adapter",
    "build_github_workspace_provisioner",
    "build_discord_event",
    "build_email_event",
    "build_linear_event",
    "build_notion_event",
    "build_slack_event",
    "build_telegram_event",
    "build_trello_event",
    "github_remote_url",
    "resolve_github_binding",
    "run_branch_name",
]
