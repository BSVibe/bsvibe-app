"""/api/v1/deliverables — read API for Deliverable rows + B12b retract.

Read-mostly: deliverables are *produced* by the agent loop / workers on a
verified run (Bundle G), never directly created via HTTP. The PWA Brief's
"recently shipped" reads this to surface real artifacts.

B12b adds one MUTATING endpoint: ``POST /{deliverable_id}/retract`` rolls a
delivered direct-mode artifact back by calling the originating plugin's
``@p.compensate`` handler with the ``compensation_handle`` captured at
delivery time (Workflow §1.2 + §3.1 + §9). The endpoint is the only path
that flips ``retracted_at``.

The ``payload`` column is free-form JSON written by the orchestrator and shaped
``{summary, artifact_refs}``; we map it defensively (missing/odd values degrade
to ``None`` / ``[]``) so a malformed row never 500s the response model.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Protocol

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_artifact_store, get_db_session, get_workspace_id
from backend.config import get_settings
from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.base import PluginMeta, PluginRunError
from backend.extensions.plugin.context import SkillContext
from backend.extensions.plugin.runner import PluginRunner
from backend.router.accounts.crypto import CredentialCipher
from backend.storage.artifact_store import ArtifactStore, LocalFilesystemArtifactStore
from backend.workflow.application.verification_service import RETRIEVED_KNOWLEDGE_RATIONALE
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    VerificationOutcome,
    VerificationResult,
)

logger = structlog.get_logger(__name__)

router = APIRouter()

# Read cap for artifact content. A produced source file is small; this guards
# against an accidental multi-MB log/blob slipping into a JSON body. Beyond it
# the response carries the first ``_MAX_CONTENT_BYTES`` decoded as text with
# ``truncated: true`` so the viewer can show a calm "showing the first part"
# note rather than streaming an unbounded payload.
_MAX_CONTENT_BYTES = 256 * 1024


class DeliverableResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    deliverable_type: DeliverableType
    summary: str | None = None
    artifact_refs: list[str] = []
    artifact_uri: str | None = None
    diff_url: str | None = None
    # B4 trust-integrity: True ONLY when a PASSED VerificationResult exists for
    # the producing run. The founder-facing "verified" badge MUST derive from
    # this backend-authoritative flag, never from a Deliverable merely existing.
    # Defaults False so a hollow row (no PASSED proof) reads honestly as
    # unverified / needs-review rather than a green "verified".
    verified: bool = False
    created_at: datetime


class VerificationReport(BaseModel):
    """One VerificationResult — the "how BSVibe checked this" proof.

    ``contract`` is the work LLM's declared list of checks (the checks BSVibe
    promised to run) and ``result`` is the execution outcome of running them;
    both are free-form JSON (shape varies by verifier), so they are surfaced
    verbatim and rendered defensively by the report view.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    outcome: VerificationOutcome
    contract: dict[str, Any] = {}
    result: dict[str, Any] = {}
    created_at: datetime


class DeliverableReportResponse(BaseModel):
    """The glass-box proof for one shipped deliverable: the founder's original
    request, the artifact, and the verification(s) recorded for its producing
    run. ``request`` is the founder's Direction that led to this work (pulled
    from the producing run's free-form payload), so the report reads as a
    document — request → what was built → how it was checked. ``None`` when the
    run carries no recorded intent."""

    model_config = ConfigDict(extra="forbid")

    deliverable: DeliverableResponse
    request: str | None = None
    # B4 trust-integrity: True ONLY when at least one PASSED VerificationResult is
    # recorded for the producing run (mirrors ``deliverable.verified``). The
    # report's "verified" / "This is verified" signal derives from this, so a
    # deliverable without a PASSED proof reads as needs-review, not verified.
    verified: bool = False
    verifications: list[VerificationReport] = []
    # G2 "근거 포함 답변": the BSage knowledge the agent referenced for this work
    # — promoted canon patterns, prior resolved decisions, and prior rejections
    # the retriever folded into the verify contract. Surfaced as a first-class
    # section (deduped, first-seen order) so the founder sees WHAT past docs /
    # decisions informed the answer, separate from the verification checklist.
    # Empty when nothing was retrieved — never a fabricated reference.
    references: list[str] = []


def _request_text_of(payload: dict[str, Any]) -> str | None:
    """Pull the founder's Direction out of the producing run's free-form payload
    (``intent_text`` from intake, or ``text``), the same keys the run-detail
    trigger context reads. ``None`` when neither is a non-empty string."""
    for key in ("intent_text", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class ArtifactContentResponse(BaseModel):
    """The produced CONTENT of one artifact file, read-only.

    Served from the persisted run workspace
    (``<run_workspace_root>/<run_id>/<ref>``) so the founder can SEE what the
    agent actually wrote — not just a filename or a (often-null) git link.

    ``content`` is the file decoded as UTF-8 text with ``errors="replace"``
    (lossy but never throws), capped at 256 KiB. ``truncated`` flags that the
    file was larger than the cap (only the leading bytes are returned).
    ``binary`` flags a non-text file, in which case ``content`` is a short
    "binary file, N bytes" note rather than the raw bytes.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str
    content: str
    truncated: bool = False
    binary: bool = False


def _summary_of(payload: dict[str, Any]) -> str | None:
    """Pull a string ``summary`` out of the free-form payload, else ``None``."""
    value = payload.get("summary")
    return value if isinstance(value, str) else None


def _artifact_refs_of(payload: dict[str, Any]) -> list[str]:
    """Pull a list of string ``artifact_refs`` out of the payload, else ``[]``."""
    value = payload.get("artifact_refs")
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _to_response(row: Deliverable, *, verified: bool = False) -> DeliverableResponse:
    payload = row.payload if isinstance(row.payload, dict) else {}
    return DeliverableResponse(
        id=row.id,
        run_id=row.run_id,
        workspace_id=row.workspace_id,
        deliverable_type=row.deliverable_type,
        summary=_summary_of(payload),
        artifact_refs=_artifact_refs_of(payload),
        artifact_uri=row.artifact_uri,
        diff_url=row.diff_url,
        verified=verified,
        created_at=row.created_at,
    )


async def _verified_run_ids(
    session: AsyncSession, workspace_id: uuid.UUID, run_ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    """The subset of ``run_ids`` that have at least one PASSED VerificationResult.

    B4 defense-in-depth: a run is "verified" ONLY when a real PASSED
    :class:`VerificationResult` row exists — never inferred from a Deliverable
    existing. One indexed query covers the whole listing page. An empty input
    short-circuits to an empty set (no needless query)."""
    if not run_ids:
        return set()
    stmt = select(VerificationResult.run_id).where(
        VerificationResult.workspace_id == workspace_id,
        VerificationResult.run_id.in_(run_ids),
        VerificationResult.outcome == VerificationOutcome.PASSED,
    )
    result = await session.execute(stmt)
    return set(result.scalars().all())


async def _run_is_verified(
    session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> bool:
    """True iff the run has at least one PASSED VerificationResult (single row)."""
    return bool(await _verified_run_ids(session, workspace_id, {run_id}))


def _references_of(verifications: list[VerificationReport]) -> list[str]:
    """The referenced-knowledge statements across a run's verifications (G2).

    Pulls the criteria of every judge check stamped with
    :data:`~backend.workflow.application.verification_service.RETRIEVED_KNOWLEDGE_RATIONALE`
    (the retriever's canon / prior-decision / prior-rejection fold), deduped in
    first-seen order. A run may record several verifications (re-attempts), so
    the same statement can recur — it surfaces once. Defensive against malformed
    contract JSON: any non-conforming shape contributes nothing, never raises."""
    references: list[str] = []
    seen: set[str] = set()
    for verification in verifications:
        checks = verification.contract.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            if check.get("rationale") != RETRIEVED_KNOWLEDGE_RATIONALE:
                continue
            criteria = check.get("criteria")
            if not isinstance(criteria, list):
                continue
            for item in criteria:
                statement = str(item).strip()
                if statement and statement not in seen:
                    seen.add(statement)
                    references.append(statement)
    return references


def _to_verification(row: VerificationResult) -> VerificationReport:
    contract = row.contract if isinstance(row.contract, dict) else {}
    result = row.result if isinstance(row.result, dict) else {}
    return VerificationReport(
        id=row.id,
        outcome=row.outcome,
        contract=contract,
        result=result,
        created_at=row.created_at,
    )


@router.get("")
async def list_deliverables(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    run_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[DeliverableResponse]:
    """List recent Deliverable rows for the workspace, newest first.

    Optional ``run_id`` narrows to one run's deliverables.
    """
    limit = max(1, min(limit, 200))
    stmt = select(Deliverable).where(Deliverable.workspace_id == workspace_id)
    if run_id is not None:
        stmt = stmt.where(Deliverable.run_id == run_id)
    stmt = stmt.order_by(Deliverable.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    verified = await _verified_run_ids(session, workspace_id, {row.run_id for row in rows})
    return [_to_response(row, verified=row.run_id in verified) for row in rows]


@router.get("/{deliverable_id}")
async def get_deliverable(
    deliverable_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DeliverableResponse:
    """Fetch one Deliverable by id, scoped to the caller's workspace."""
    row = await session.get(Deliverable, deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )
    verified = await _run_is_verified(session, workspace_id, row.run_id)
    return _to_response(row, verified=verified)


@router.get("/{deliverable_id}/report")
async def get_deliverable_report(
    deliverable_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DeliverableReportResponse:
    """The glass-box proof for one deliverable, scoped to the caller's workspace.

    Returns the deliverable (summary, artifact_refs, artifact_uri, diff_url,
    type, created_at) PLUS the ``VerificationResult`` rows recorded for its
    producing ``run_id`` — each carrying the declared ``contract`` (the checks
    BSVibe promised to run), the ``result`` of running them, and the ``outcome``
    verdict. 404 when the deliverable isn't in the caller's workspace. A run
    with no verification yields a calm empty list rather than erroring.
    """
    row = await session.get(Deliverable, deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )
    stmt = (
        select(VerificationResult)
        .where(
            VerificationResult.run_id == row.run_id,
            VerificationResult.workspace_id == workspace_id,
        )
        .order_by(VerificationResult.created_at.asc())
    )
    result = await session.execute(stmt)
    verifications = [_to_verification(v) for v in result.scalars().all()]
    # B4 trust-integrity: the report is "verified" ONLY when a real PASSED
    # VerificationResult is among the run's recorded verifications — never
    # inferred from the Deliverable existing. A hollow deliverable (none, or only
    # failed/inconclusive) reads as needs-review, honestly.
    verified = any(v.outcome == VerificationOutcome.PASSED for v in verifications)

    # The founder's Direction that led to this work — pulled from the producing
    # run's free-form payload so the report reads request → built → checked. A
    # missing run (cleaned history) degrades to no request, never a 500.
    run = await session.get(ExecutionRun, row.run_id)
    request = (
        _request_text_of(run.payload)
        if run is not None and run.workspace_id == workspace_id and isinstance(run.payload, dict)
        else None
    )

    return DeliverableReportResponse(
        deliverable=_to_response(row, verified=verified),
        request=request,
        verified=verified,
        verifications=verifications,
        references=_references_of(verifications),
    )


def _looks_binary(raw: bytes) -> bool:
    """Heuristic binary sniff: a NUL byte in the inspected prefix → binary.

    Mirrors git's own "is this a text file" test (a NUL in the first 8 KiB).
    Cheap, dependency-free, and deliberately conservative — a stray NUL makes
    us report metadata-only rather than dumping mojibake into a JSON string.
    """
    return b"\x00" in raw[:8192]


async def _read_from_product_main(
    session: AsyncSession, run_id: uuid.UUID, ref: str
) -> bytes | None:
    """Read ``ref`` from the run's product workspace main checkout, or ``None``.

    The W2 ship-time merge lands the run's files under
    ``<product_workspace_root>/<product_id>/`` (the product repo's main checkout)
    and then removes the per-run worktree. A reused
    :class:`LocalFilesystemArtifactStore` rooted at ``product_workspace_root`` and
    keyed by ``product_id`` resolves ``<root>/<product_id>/<ref>`` with the SAME
    centralized traversal guard. ``None`` when the run has no product_id (nothing
    to fall back to) or the file is genuinely absent — the caller maps it to 404.
    """
    run = await session.get(ExecutionRun, run_id)
    if run is None or run.product_id is None:
        return None
    product_store = LocalFilesystemArtifactStore(Path(get_settings().product_workspace_root))
    try:
        return product_store.read_bytes(run.product_id, ref)
    except (ValueError, FileNotFoundError, IsADirectoryError):
        return None


@router.get("/{deliverable_id}/artifacts/{ref:path}")
async def get_deliverable_artifact(
    deliverable_id: uuid.UUID,
    ref: str,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
) -> ArtifactContentResponse:
    """Serve one artifact file's CONTENT, read-only, scoped to the caller.

    The file is read from the deliverable's PERSISTED run workspace via the
    per-run :class:`ArtifactStore` (today an FS-backed store under
    ``<run_workspace_root>/<run_id>/<ref>``; tomorrow R2/S3 with no call-site
    change). The orchestrator/worker drives each run inside that dir and the
    work LLM's writes land there, so no orchestrator/git change is needed to
    surface real content.

    Security (all 404 — never leak existence/contents across the boundary):
      * workspace scope — the deliverable must belong to the caller's workspace;
      * ref whitelist — ``ref`` MUST be one of the deliverable's own
        ``payload.artifact_refs`` (arbitrary paths are refused outright);
      * path traversal — the store's centralized guard refuses any ref that
        resolves outside the run dir (an absolute path / ``../`` segment);
      * missing file — a cleaned run dir / absent file 404s calmly.

    Content is UTF-8 with ``errors="replace"``, capped at 256 KiB
    (``truncated: true`` past the cap). A binary file yields a short
    "binary file, N bytes" note (``binary: true``) instead of raw bytes.
    """
    row = await session.get(Deliverable, deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )

    # Ref whitelist: only the deliverable's own declared artifact_refs are
    # serveable — never an arbitrary path the caller supplies.
    payload = row.payload if isinstance(row.payload, dict) else {}
    if ref not in _artifact_refs_of(payload):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="artifact not found for this deliverable",
        )

    try:
        raw = store.read_bytes(row.run_id, ref)
    except ValueError as exc:
        # Traversal / absolute ref — refused by the store's centralized guard.
        # Surface as 404 (never leak existence across the boundary).
        logger.debug("artifact_traversal_refused", run_id=str(row.run_id), ref=ref, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="artifact not found for this deliverable",
        ) from exc
    except FileNotFoundError as exc:
        # W1/W2: a product-bound run's worktree is REMOVED after auto-ship merges
        # it to the product's main, so the produced file no longer lives in the
        # run dir — it lives in the product workspace main checkout. Fall back
        # there before declaring the content gone (else the Files viewer can
        # never open a shipped product run's files). Non-product runs (no main to
        # fall back to) keep the calm 404.
        fallback = await _read_from_product_main(session, row.run_id, ref)
        if fallback is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="artifact content is no longer available",
            ) from exc
        raw = fallback
    except IsADirectoryError as exc:
        # ``ref`` resolves to a directory inside the run dir (e.g. ``src/``).
        # Calm 404 — not a file, no content to serve.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="artifact content is no longer available",
        ) from exc
    if _looks_binary(raw):
        return ArtifactContentResponse(
            ref=ref,
            content=f"Binary file, {len(raw)} bytes — not shown.",
            truncated=False,
            binary=True,
        )

    truncated = len(raw) > _MAX_CONTENT_BYTES
    text = raw[:_MAX_CONTENT_BYTES].decode("utf-8", errors="replace")
    return ArtifactContentResponse(
        ref=ref,
        content=text,
        truncated=truncated,
        binary=False,
    )


# ---------------------------------------------------------------------------
# B12b — retract (compensate a delivered direct-mode artifact)
# ---------------------------------------------------------------------------


class RetractHandler(Protocol):
    """The runtime hand-off that actually calls a plugin's ``@p.compensate``.

    Stubbed in tests via the :func:`get_retract_handler` dependency override;
    production wires :class:`PluginRetractHandler` which loads the plugin
    registry, resolves the workspace's connector account for the named plugin,
    decrypts its secret, and dispatches through :class:`PluginRunner`.
    """

    async def compensate(
        self,
        *,
        plugin: str,
        artifact_type: str,
        handle: dict[str, Any],
        workspace_id: uuid.UUID,
    ) -> dict[str, Any]: ...


class PluginRetractHandler:
    """Production :class:`RetractHandler` — dispatches through the plugin runner.

    Per stored entry the handler:

    1. Looks up the plugin by name in the loaded registry.
    2. Resolves the workspace's active ``connector_account`` for that plugin
       (the same row the delivery used), decrypts its secret, builds a
       :class:`SkillContext` mirroring the delivery-time one.
    3. Calls :meth:`PluginRunner.dispatch_compensate` with the captured handle.

    Plugin or connector_account missing → :class:`PluginRunError` (the endpoint
    surfaces this as 502, the row is NOT marked retracted so the operator can
    see + retry). Handlers are idempotent (Workflow §9), so a stale handle
    yielding "already gone" is reported as success.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        plugins_by_name: dict[str, PluginMeta],
        cipher: CredentialCipher,
        runner: PluginRunner | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._plugins_by_name = plugins_by_name
        self._cipher = cipher
        self._runner = runner or PluginRunner()

    async def compensate(
        self,
        *,
        plugin: str,
        artifact_type: str,
        handle: dict[str, Any],
        workspace_id: uuid.UUID,
    ) -> dict[str, Any]:
        meta = self._plugins_by_name.get(plugin)
        if meta is None:
            raise PluginRunError(f"compensate: plugin {plugin!r} not loaded")
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ConnectorAccountRow).where(
                            ConnectorAccountRow.workspace_id == workspace_id,
                            ConnectorAccountRow.connector == plugin,
                            ConnectorAccountRow.is_active.is_(True),
                        )
                    )
                )
                .scalars()
                .first()
            )
        credentials: dict[str, Any] = {}
        config: dict[str, Any] = {}
        if row is not None:
            credentials = {"token": self._cipher.decrypt(row.signing_secret_ciphertext)}
            config = dict(row.delivery_config or {})
        ctx = SkillContext(llm=_NoLlm(), config=config, logger=logger, credentials=credentials)
        result = await self._runner.dispatch_compensate(
            meta,
            artifact_type=artifact_type,
            context=ctx,
            handle=handle,
        )
        return result if isinstance(result, dict) else {"result": result}


class _NoLlm:
    """A no-op LLM for the compensate :class:`SkillContext`.

    Compensation handlers call external APIs to revert artifacts — they should
    never invoke the LLM. Calling this raises rather than silently no-opping.
    Mirrors :class:`backend.workflow.application.delivery.connector_dispatch._NoLlm`.
    """

    async def chat(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("compensate must not call the LLM")


async def get_retract_handler() -> RetractHandler:  # pragma: no cover — overridden in tests
    """Production :class:`RetractHandler` dependency.

    Loads the plugin registry (same path the delivery worker uses) + builds a
    :class:`PluginRetractHandler` over the request-scoped session factory and
    settings-derived :class:`CredentialCipher`. Tests override this with an
    in-test stub so a unit run never touches the loader / KMS.
    """
    from backend.api.deps import _get_session_factory  # noqa: PLC0415 — avoid import cycle
    from backend.extensions.plugin.loader import PluginLoader  # noqa: PLC0415
    from backend.router.accounts.crypto import _key_from_settings  # noqa: PLC0415

    # Lift R1 (v8 §D38) — connector plugins live at repo-root ``plugin/`` —
    # walk up from this module to find it. Path resolution is one-time per
    # request scope and cheap; noqa ASYNC240 mirrors the worker default at
    # ``backend.workflow.infrastructure.workers.run._PLUGINS_IMPLEMENTATIONS_DIR``.
    plugin_dir = Path(__file__).resolve().parents[3] / "plugin"  # noqa: ASYNC240
    loader = PluginLoader(plugin_dir)
    registry = await loader.load_all()
    return PluginRetractHandler(
        session_factory=_get_session_factory(),
        plugins_by_name=dict(registry),
        cipher=CredentialCipher(_key_from_settings()),
    )


class RetractedCompensationEntry(BaseModel):
    """One per-stored-handle dispatch outcome (Workflow §3.1)."""

    model_config = ConfigDict(extra="forbid")

    plugin: str
    artifact_type: str
    output: dict[str, Any] = {}


class RetractResponse(BaseModel):
    """The retract endpoint's response shape (Workflow §1.2)."""

    model_config = ConfigDict(extra="forbid")

    deliverable_id: uuid.UUID
    retracted: bool
    retracted_at: datetime
    # B12b — True iff the row was ALREADY retracted before this call (200
    # no-op, the API short-circuited and the per-handle compensate dispatches
    # did NOT re-run). False on the first successful retract. Lets the founder
    # UI render "already retracted" cleanly vs. "just retracted".
    already_retracted: bool = False
    compensated: list[RetractedCompensationEntry] = []


@router.post("/{deliverable_id}/retract")
async def retract_deliverable(
    deliverable_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    handler: Annotated[RetractHandler, Depends(get_retract_handler)],
) -> RetractResponse:
    """Roll a delivered direct-mode artifact back (B12b / Workflow §1.2 + §9).

    Reads the Deliverable's ``compensation_handles`` (populated at delivery
    time from each successful outbound action's ``compensation_handle``) and
    calls the originating plugin's ``@p.compensate`` handler with each.

    Error semantics (operator-visible; the row is mutated ONLY on success):

    * ``404 not_found`` — unknown id, or the deliverable belongs to another
      workspace (existence is never leaked across the boundary).
    * ``400 no_compensation_handle`` — the row carries no handles (pre-B12b or
      every outbound opted out of compensation); nothing to revert.
    * ``502 compensate_failed`` — at least one compensate dispatch raised; the
      row is NOT marked retracted, so the operator can retry. Idempotent
      plugin handlers re-tolerate the second call.
    * ``200 already_retracted`` — re-retracting an already-retracted row is a
      short-circuit no-op (the plugin handlers are idempotent, but the API
      avoids even attempting them).
    """
    row = await session.get(Deliverable, deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )

    # Idempotency: already retracted → 200 no-op (don't fire compensate twice).
    if row.retracted_at is not None:
        return RetractResponse(
            deliverable_id=deliverable_id,
            retracted=True,
            retracted_at=row.retracted_at,
            already_retracted=True,
            compensated=[],
        )

    handles = list(row.compensation_handles or [])
    if not handles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no_compensation_handle: deliverable has no captured compensation handles",
        )

    compensated: list[RetractedCompensationEntry] = []
    for entry in handles:
        plugin = str(entry.get("plugin") or "")
        artifact_type = str(entry.get("artifact_type") or "")
        handle = entry.get("handle")
        if not plugin or not isinstance(handle, dict):
            # Malformed stored entry — surface as a 502 so the operator sees it
            # rather than silently skipping a delivered artifact.
            logger.warning(
                "retract_malformed_entry",
                deliverable_id=str(deliverable_id),
                entry=entry,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"compensate_failed: malformed compensation entry {entry!r}",
            )
        try:
            output = await handler.compensate(
                plugin=plugin,
                artifact_type=artifact_type,
                handle=handle,
                workspace_id=workspace_id,
            )
        except Exception as exc:  # noqa: BLE001 — surface as 502 + log; do NOT mark retracted
            logger.warning(
                "retract_compensate_failed",
                deliverable_id=str(deliverable_id),
                plugin=plugin,
                artifact_type=artifact_type,
                error=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"compensate_failed: {exc}",
            ) from exc
        compensated.append(
            RetractedCompensationEntry(
                plugin=plugin,
                artifact_type=artifact_type,
                output=output if isinstance(output, dict) else {"result": output},
            )
        )

    # All handlers succeeded — flip retracted_at.
    now = datetime.now(tz=UTC)
    row.retracted_at = now
    await session.commit()
    logger.info(
        "deliverable_retracted",
        deliverable_id=str(deliverable_id),
        workspace_id=str(workspace_id),
        compensated=len(compensated),
    )
    return RetractResponse(
        deliverable_id=deliverable_id,
        retracted=True,
        retracted_at=now,
        already_retracted=False,
        compensated=compensated,
    )
