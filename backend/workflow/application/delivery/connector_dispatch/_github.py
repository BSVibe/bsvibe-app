"""github delivery special case (Lift §17.7).

github is the one delivery target that needs a real DIFF, so it is NOT a simple
event builder. Two pieces live here:

1. :func:`build_github_workspace_provisioner` — the run-setup hook that clones
   the workspace's github target into the run's workspace dir on a fresh
   ``bsvibe/run-<id>`` branch, so the agent's file edits operate on a real
   checkout a PR diff can be built from.
2. :func:`deliver_github` — the per-deliverable handler: commit_all → push →
   open the github plugin's ``open_pr`` action. No diff → clean no-op success
   (so a non-code run in a github workspace doesn't open an empty PR).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.auth.resolve import resolve_connector_credentials
from backend.extensions.plugin.base import PluginMeta
from backend.extensions.plugin.runner import PluginRunner
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.domain.delivery import ActionResult
from backend.workflow.infrastructure.delivery.git_ops import GitOps

from ._builders import _split_summary
from ._context import _build_context
from ._resolver import GithubBinding, resolve_github_binding

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
        # OAuth token (if the workspace connected via "Connect with GitHub")
        # takes precedence over the legacy signing secret — both clone, push,
        # and PR creation must use the SAME resolved credential.
        creds = await resolve_connector_credentials(
            session, account=binding.account, cipher=_resolve_cipher()
        )
        token = creds["token"]
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


@dataclass(slots=True)
class GithubDeliveryDeps:
    """Per-adapter dependencies the github delivery handler reads.

    Bundled so :func:`deliver_github` can be called from the adapter without a
    long parameter list — the adapter holds these as fields and forwards the
    bundle.
    """

    cipher: CredentialCipher
    plugins_by_name: dict[str, PluginMeta]
    workspace_root: Path | None
    git_ops: GitOps
    remote_url_for: Callable[[str], str]
    runner: PluginRunner
    # Opens a fresh session to resolve the github API credential (OAuth token
    # else legacy secret) at delivery time — the binding was resolved in an
    # already-closed session, so credential resolution needs its own.
    session_factory: async_sessionmaker[AsyncSession]


async def deliver_github(
    *,
    deps: GithubDeliveryDeps,
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
    if deps.workspace_root is None or run_id is None:
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
    checkout = deps.workspace_root / str(run_id)
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

    branch = run_branch_name(run_id)
    summary = str(content.get("summary") or "")
    title, body = _split_summary(summary)

    # 1. Commit the agent's file edits. No new working-tree changes is OK —
    # the verifier's W2 ``commit_worktree`` step (run.product_id != None +
    # real worktree) may have already committed every agent edit on top of
    # the base branch. Lift E41 — only treat the run as a clean no-op when
    # ``commit_all`` made no new commit AND the branch is NOT ahead of base.
    # Otherwise still push + open the PR (the W2 commit IS the deliverable).
    committed = await deps.git_ops.commit_all(checkout, title)
    if not committed:
        ahead = await deps.git_ops.is_ahead_of_base(checkout, binding.base_branch)
        if not ahead:
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
        logger.info(
            "github_delivery_using_w2_commit",
            workspace_id=str(workspace_id),
            deliverable_id=str(deliverable_id),
            run_id=str(run_id),
        )

    # 2. Resolve the github API credential — OAuth token (Connect with GitHub)
    #    takes precedence over the legacy signing secret. Resolved AFTER the
    #    no-op check so a no-change run never opens a DB session. A fresh
    #    session is needed because the binding came from an already-closed one.
    async with deps.session_factory() as session:
        creds = await resolve_connector_credentials(
            session, account=binding.account, cipher=deps.cipher
        )
        # Persist any token refresh resolve performed under the hood.
        await session.commit()
    token = creds["token"]

    # 3. Push the branch to the (real-or-test) remote.
    remote_url = deps.remote_url_for(binding.repo)
    await deps.git_ops.push(checkout, branch, token=token)

    # 4. Open the PR via the github plugin's open_pr action. Routing
    #    (repo/base) is the stable founder-set config; head is the run
    #    branch; title/body come from the deliverable summary (content).
    plugin = deps.plugins_by_name.get("github")
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
        result = await deps.runner.dispatch_action(
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


__all__ = [
    "GithubDeliveryDeps",
    "build_github_workspace_provisioner",
    "deliver_github",
    "github_remote_url",
    "run_branch_name",
]
