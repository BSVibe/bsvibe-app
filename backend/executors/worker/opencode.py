"""The ``opencode`` executor — HTTP against a long-running ``opencode serve`` (Lift E17).

Pre-E17 this executor spawned ``opencode run --format json`` per task. The
dogfood after E16 found this fundamentally too slow for chat-shaped ingest
calls: a trivial 1-line prompt wall-clocked at 8 hours, and even small ingest
chunks (1–3 seeds, ~2.5 KB) wall-clocked at 20+ minutes. The cost was startup
tax (workspace scan, plugin load, tool registry init, agent runtime spin-up)
paid every single call. The runtime is meant to be a long-lived TUI/server,
not a per-call subprocess.

E17 keeps a single ``opencode serve`` daemon up per worker (managed by
:mod:`backend.executors.worker.opencode_server`) and hits its HTTP surface
once per task:

1. ``POST /session`` → returns the new session id.
2. ``POST /session/{id}/message`` with body
   ``{"system": <str>, "parts": [{"type":"text","text": <prompt>}],
   "agent": "plan", "model": <optional>}``.

The dogfood-verified wire shape is critical: ``system`` is a TOP-LEVEL key
alongside ``parts``, NOT a system-role message inside ``parts``. The
response's text part carries the LLM output.

Robustness:

* ``opencode_request_timeout_s`` caps each call wall-clock (default 600 s).
* On non-2xx → terminal error chunk (no crash).
* On :class:`httpx.ConnectError` (daemon died mid-run) → ask the server
  module to re-spawn once, then retry once. A second failure surfaces as
  a terminal error chunk.
* :class:`asyncio.CancelledError` propagation: the executor aborts the
  open session via ``POST /session/{id}/abort`` so the serve daemon stops
  the LLM call server-side, then re-raises CancelledError so the worker
  loop's cancel chain (E14/E15) stays correct.

NO subprocess path remains; reintroducing it would re-open the dogfood bug
this lift exists to close. :func:`test_no_subprocess_exec_used` enforces that.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from backend.executors.worker import opencode_server
from backend.executors.worker.config import get_worker_settings
from backend.executors.worker.executors import ExecutionChunk

logger = structlog.get_logger(__name__)


class OpenCodeExecutor:
    """Stream from ``opencode serve``'s HTTP message surface."""

    def __init__(
        self,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # ``http_transport`` is the test seam — production passes ``None`` and
        # the real network is used. The transport is stashed (not the client)
        # because each ``execute`` opens + closes its own client; sharing a
        # client would couple lifetimes across cancel boundaries.
        self._transport = http_transport
        self._settings = get_worker_settings()

    def supported_task_types(self) -> list[str]:
        return ["opencode"]

    async def execute(self, prompt: str, context: dict[str, Any]) -> AsyncIterator[ExecutionChunk]:
        system = context.get("system") or ""
        model = context.get("model") or None
        timeout_s = self._settings.opencode_request_timeout_s

        url = opencode_server.get_serve_url()
        if not url:
            # The worker daemon never started the serve subprocess. Surface
            # a clear error rather than silently failing — the dispatch
            # layer needs a terminal error to record on the task row.
            yield ExecutionChunk(
                done=True,
                error=(
                    "opencode serve URL singleton is unset — the worker "
                    "daemon did not start the serve subprocess at boot"
                ),
            )
            return

        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": prompt}],
            "agent": self._settings.opencode_serve_agent,
        }
        if system:
            body["system"] = system
        # Lift E24 — opencode's HTTP API expects ``model`` as an object
        # ``{"providerID": ..., "modelID": ...}``, not a plain string. The
        # founder's RunRoutingRule / ModelAccount.litellm_model carries the
        # vendor-prefixed id ``opencode-go/qwen3.6-plus``; split at the FIRST
        # ``/`` to derive provider + model. An id without a ``/`` cannot be
        # split (we'd guess wrong and opencode would 400) — omit ``model``
        # entirely and let the daemon's default fire.
        if model and "/" in model:
            provider_id, _, model_id = model.partition("/")
            body["model"] = {"providerID": provider_id, "modelID": model_id}

        # Mutable holder so the inner helper can publish the session_id back
        # to the cancel handler the moment it is created — without this, a
        # cancellation that fires during the message POST would lose the
        # session_id needed to abort it server-side.
        session_holder: dict[str, str] = {}
        client = self._client(url, timeout_s)
        abort_task: asyncio.Task[None] | None = None
        try:
            try:
                resp = await self._call_with_respawn(client, body, session_holder)
            except asyncio.CancelledError:
                sid = session_holder.get("id")
                if sid:
                    # Lift E17 — fire the abort POST so the serve daemon
                    # stops the LLM call server-side. We schedule it on the
                    # loop here and ``shield`` it in the ``finally`` block;
                    # the shielded task is allowed to complete even though
                    # our outer task is being cancelled.
                    abort_task = asyncio.create_task(self._do_abort(url, sid, timeout_s))
                raise
            except httpx.HTTPError as exc:
                yield ExecutionChunk(done=True, error=str(exc))
                return
            except OpenCodeHttpError as exc:
                yield ExecutionChunk(done=True, error=str(exc))
                return
        finally:
            if abort_task is not None:
                try:
                    await asyncio.shield(abort_task)
                except asyncio.CancelledError:
                    # Our outer task is being cancelled — the shielded
                    # abort task continues running on the loop until it
                    # lands (or its 5 s internal timeout fires).
                    pass
                except Exception:  # noqa: BLE001 — cleanup best-effort
                    logger.warning(
                        "opencode_session_abort_failed",
                        session_id=session_holder.get("id"),
                        exc_info=True,
                    )
            await client.aclose()

        text = _extract_text(resp)
        if text:
            yield ExecutionChunk(delta=text, raw=resp)
        yield ExecutionChunk(
            done=True,
            raw={"info": resp.get("info")},
        )

    # ── Internals ───────────────────────────────────────────────────────────

    def _client(self, base_url: str, timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            transport=self._transport,
            timeout=timeout_s,
        )

    async def _create_session(self, client: httpx.AsyncClient) -> str:
        res = await client.post("/session", json={})
        if res.status_code >= 300:
            raise OpenCodeHttpError(
                f"POST /session returned {res.status_code}: {_truncate(res.text)}"
            )
        data = res.json()
        sid = data.get("id")
        if not isinstance(sid, str) or not sid:
            raise OpenCodeHttpError(
                f"POST /session response missing 'id' field: {_truncate(res.text)}"
            )
        return sid

    async def _call_with_respawn(
        self,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        session_holder: dict[str, str],
    ) -> dict[str, Any]:
        """Create a session + post the message. On ConnectError → re-spawn once + retry.

        Returns the response JSON. The created session id is published into
        ``session_holder["id"]`` the moment ``POST /session`` returns, so a
        cancellation that fires during the message POST can still find the
        sid to abort server-side.
        """
        try:
            sid = await self._create_session(client)
            session_holder["id"] = sid
            return await self._post_message(client, sid, body)
        except httpx.ConnectError as exc:
            logger.warning("opencode_serve_dead_retrying", error=str(exc))
            await opencode_server.ensure_serve_running(self._settings)
            sid = await self._create_session(client)
            session_holder["id"] = sid
            return await self._post_message(client, sid, body)

    async def _post_message(
        self, client: httpx.AsyncClient, session_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        res = await client.post(f"/session/{session_id}/message", json=body)
        if res.status_code >= 300:
            raise OpenCodeHttpError(
                f"POST /session/{session_id}/message returned "
                f"{res.status_code}: {_truncate(res.text)}"
            )
        data: dict[str, Any] = res.json()
        return data

    async def _do_abort(self, base_url: str, session_id: str, timeout_s: float) -> None:
        """POST ``/session/{id}/abort`` on a fresh client.

        Uses its own ``httpx.AsyncClient`` (not the cancelled task's client)
        so the abort POST is not racing the outer ``client.aclose()`` in
        :meth:`execute`'s ``finally``. Caller schedules this as a separate
        task and ``shield``s it from the outer cancellation.
        """
        del timeout_s  # the abort POST gets its own short timeout
        try:
            async with httpx.AsyncClient(
                base_url=base_url, transport=self._transport, timeout=5.0
            ) as abort_client:
                await abort_client.post(f"/session/{session_id}/abort")
            logger.info("opencode_session_aborted", session_id=session_id)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.warning("opencode_session_abort_failed", session_id=session_id, exc_info=True)


# ── Helpers ─────────────────────────────────────────────────────────────────


class OpenCodeHttpError(RuntimeError):
    """Non-2xx HTTP from the serve daemon, or a malformed response body."""


def _extract_text(resp: dict[str, Any]) -> str:
    """Pick the first text part out of a serve message response.

    Server response shape::

        {"parts": [{"type": "text", "text": "<llm output>"}, ...], "info": {...}}

    Concatenates every text part in order. Non-text parts (tool calls etc.) are
    skipped — the chat-shaped caller wants the assistant's natural-language reply.
    """
    parts = resp.get("parts") or []
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def _truncate(text: str, max_len: int = 500) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


__all__ = ["OpenCodeExecutor", "OpenCodeHttpError"]
