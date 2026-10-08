"""#954 — LLM spend with no run to accrue to is kept in a workspace ledger.

#953 sealed every channel that has a run and made the rest — knowledge ingest
(one call PER CHUNK), settle extraction, concept framing / labels, routing-rule
compile — report under ``llm_usage_unattributed``. Measured 2026-10-08: the prod
log archive keeps 20 container lifetimes, about a day at the current deploy
rate, so "aggregate the event once traffic has built up" could never be done.

형님 ruled (2026-10-08): keep these in a WORKSPACE ledger — durable, countable,
and never attached to a run they did not belong to. These tests pin the writer
and every seam that feeds it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from backend.data.rls import workspace_session_scope
from backend.dispatch.adapter import ChatResponse
from backend.workflow.application.runtime.dispatcher import (
    UNATTRIBUTED_USAGE_EVENT,
    _ResolverCompileLlm,
    _ResolverFrameLlm,
    record_unattributed_usage,
)
from backend.workflow.infrastructure.db import UnattributedLlmUsage

from .._support import shared_file_sessionmaker

pytestmark = pytest.mark.asyncio


class _Adapter:
    def __init__(self, *, prompt: int, completion: int, content: str = "ok") -> None:
        self._response = ChatResponse(
            content=content, usage_prompt_tokens=prompt, usage_completion_tokens=completion
        )

    async def chat(self, **_: Any) -> ChatResponse:
        return self._response


async def _rows(factory, ws: uuid.UUID) -> list[UnattributedLlmUsage]:
    async with factory() as s, workspace_session_scope(s, ws):
        return list((await s.scalars(select(UnattributedLlmUsage))).all())


async def test_the_writer_keeps_a_row_and_still_logs_the_event() -> None:
    ws = uuid.uuid4()
    async with shared_file_sessionmaker() as factory:
        with capture_logs() as logs:
            await record_unattributed_usage(
                factory,
                site="knowledge.ingest",
                workspace_id=ws,
                usage_prompt_tokens=900,
                usage_completion_tokens=80,
                product_id="p-1",
            )
        rows = await _rows(factory, ws)

    assert [(r.site, r.usage_prompt_tokens, r.usage_completion_tokens) for r in rows] == [
        ("knowledge.ingest", 900, 80)
    ]
    assert rows[0].workspace_id == ws
    assert rows[0].identity == {"product_id": "p-1"}
    assert [e["event"] for e in logs].count(UNATTRIBUTED_USAGE_EVENT) == 1


async def test_a_turn_that_cost_nothing_writes_nothing() -> None:
    ws = uuid.uuid4()
    async with shared_file_sessionmaker() as factory:
        await record_unattributed_usage(
            factory, site="x", workspace_id=ws, usage_prompt_tokens=0, usage_completion_tokens=0
        )
        assert await _rows(factory, ws) == []


async def test_a_failed_write_never_fails_the_call_that_already_spent() -> None:
    """The tokens are spent by the time this runs — losing the row is bad, losing the
    ingest that paid for it is worse."""

    def _broken() -> Any:
        raise RuntimeError("db down")

    with capture_logs() as logs:
        await record_unattributed_usage(
            _broken,  # type: ignore[arg-type]
            site="x",
            workspace_id=uuid.uuid4(),
            usage_prompt_tokens=5,
            usage_completion_tokens=1,
        )
    assert "llm_usage_unattributed_write_failed" in [e["event"] for e in logs]


async def test_the_compile_seam_writes_its_turn() -> None:
    """Ingest, bootstrap and settle extraction all run on this seam."""
    ws = uuid.uuid4()
    async with shared_file_sessionmaker() as factory:
        llm = _ResolverCompileLlm(
            adapter=_Adapter(prompt=300, completion=20),  # type: ignore[arg-type]
            workspace_id=ws,
            site="knowledge.ingest",
            session_factory=factory,
        )
        await llm.chat(system="s", messages=[{"role": "user", "content": "u"}])
        await llm.chat(system="s", messages=[{"role": "user", "content": "u"}])
        rows = await _rows(factory, ws)
    # One row per chunk call — the multiplying item stays countable.
    assert [r.usage_prompt_tokens for r in rows] == [300, 300]


async def test_the_concept_framer_writes_its_turns() -> None:
    from backend.workflow.application.runtime.settle_runtime import _RoutedConceptFramer

    ws = uuid.uuid4()
    async with shared_file_sessionmaker() as factory:
        framer = _RoutedConceptFramer(
            _ResolverFrameLlm(adapter=_Adapter(prompt=55, completion=6, content="a synthesis.")),  # type: ignore[arg-type]
            workspace_id=ws,
            session_factory=factory,
        )
        await framer.frame(concept="oauth", members=[("a", "a detail")])
        await framer.label(concept="oauth")
        rows = await _rows(factory, ws)
    assert sorted(r.identity["concept"] for r in rows) == ["oauth", "oauth"]
    assert {r.site for r in rows} == {"knowledge.canonicalization"}


async def test_the_production_framer_factory_hands_the_ledger_its_factory(monkeypatch) -> None:
    from backend.workflow.application.runtime import settle_runtime

    ws = uuid.uuid4()

    async def _resolved(*_: Any, **__: Any) -> Any:
        return type("R", (), {"adapter": _Adapter(prompt=7, completion=1, content="x")})()

    monkeypatch.setattr(settle_runtime, "resolve_via_caller", _resolved)
    async with shared_file_sessionmaker() as factory:
        framer = await settle_runtime.build_concept_framer(session_factory=factory)(workspace_id=ws)
        assert framer is not None
        await framer.label(concept="c")
        assert [r.usage_prompt_tokens for r in await _rows(factory, ws)] == [7]


async def test_the_routing_compile_writes_its_turn(monkeypatch) -> None:
    from backend.api.v1 import run_routing

    ws = uuid.uuid4()

    class _Resolver:
        def __init__(self, *_: Any, **__: Any) -> None: ...

        async def resolve_for(self, **_: Any) -> Any:
            return type("R", (), {"adapter": _Adapter(prompt=42, completion=3, content="[]")})()

    monkeypatch.setattr("backend.dispatch.resolver.ModelAccountResolver", _Resolver)
    async with shared_file_sessionmaker() as factory:
        async with factory() as request_session:
            llm = await run_routing._resolve_compile_llm(request_session, ws)
            assert llm is not None
            await llm.complete_text(system="s", user="u")
        assert [r.site for r in await _rows(factory, ws)] == ["routing.compile"]


class _Captured(Exception):
    """Stops a builder the moment it constructs the compile seam."""


def _capturing_compile_llm(seen: list[dict[str, Any]]) -> Any:
    def _build(**kwargs: Any) -> Any:
        seen.append(kwargs)
        raise _Captured

    return _build


async def _resolved_double(*_: Any, **__: Any) -> Any:
    return type("R", (), {"adapter": _Adapter(prompt=1, completion=1)})()


async def test_the_settle_extractor_hands_the_ledger_its_factory(monkeypatch) -> None:
    from backend.workflow.application.runtime import settle_runtime

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(settle_runtime, "resolve_via_caller", _resolved_double)
    monkeypatch.setattr(settle_runtime, "_ResolverCompileLlm", _capturing_compile_llm(seen))
    async with shared_file_sessionmaker() as factory:
        build = settle_runtime.build_settle_entity_extractor_factory(session_factory=factory)
        with pytest.raises(_Captured):
            await build(workspace_id=uuid.uuid4())
    assert seen[0]["session_factory"] is factory


async def test_the_bootstrap_ingest_hands_the_ledger_its_factory(monkeypatch, tmp_path) -> None:
    from backend.workflow.application.runtime import product_bootstrap_runtime as pbr

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(pbr, "resolve_via_caller", _resolved_double)
    monkeypatch.setattr(pbr, "_ResolverCompileLlm", _capturing_compile_llm(seen))
    settings = type("S", (), {"knowledge_vault_root": str(tmp_path)})()
    from backend.knowledge.facade import IngestRequest

    ws = uuid.uuid4()
    async with shared_file_sessionmaker() as factory:
        async with factory() as session:
            knowledge = pbr.build_bootstrap_knowledge(
                session=session,
                workspace_id=ws,
                settings=settings,  # type: ignore[arg-type]
                session_factory=factory,
            )
            assert knowledge is not None
            with pytest.raises(_Captured):
                await knowledge.ingest(
                    IngestRequest(workspace_id=ws, artifacts=[{"path": "a.md", "content": "x"}])
                )
    assert seen[0]["session_factory"] is factory
