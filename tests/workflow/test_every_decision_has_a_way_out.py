"""#1074 — a run never stops on a Decision the founder cannot answer.

Prod 2026-09-28, run ``cc68f583``: a schedule run stopped on
``ambiguous_model_account`` for 30 hours and nobody knew. The checkpoint read
``question=""``, ``options=null``, ``actions=null`` — and no notification went
out, because ``resolve_workspace_model_account`` wrote a raw ``Decision(...)``
instead of going through ``create_decision`` (the one place that calls the
founder). Its sibling ``no_model_account`` had the same shape.

#1105 was the same defect for ``run_token_cap_reached``. Fixing kinds one at a
time is how a third one slips in, so the guard at the bottom pins the property
for EVERY kind the code can create: a question in both languages, and either
one-click actions or options to pick from.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import WorkspaceRow
from backend.router.accounts.models import ModelAccount
from backend.workflow.application._checkpoint_shared import (
    _decision_actions,
    _decision_options,
    _question_text,
)
from backend.workflow.infrastructure.db import Decision, ExecutionRun, RunStatus

from .._support import db_engine


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(sf_: Any, *, labels: list[str]) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    ids: list[uuid.UUID] = []
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        s.add(WorkspaceRow(id=workspace_id, name="acme", safe_mode=True, language="ko"))
        await s.flush()
        for label in labels:
            account = ModelAccount(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                account_id=uuid.uuid4(),
                provider="ollama",
                label=label,
                litellm_model=f"ollama_chat/{label}",
                is_active=True,
                extra_params={},
            )
            s.add(account)
            ids.append(account.id)
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.RUNNING,
                payload={"intent_text": "weekly report"},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return workspace_id, run_id, ids


async def _unresolved(sf_: Any, workspace_id: uuid.UUID, run_id: uuid.UUID) -> Decision:
    from backend.workflow.application.runtime.account_resolution import (
        resolve_workspace_model_account,
    )

    async with sf_() as s, workspace_session_scope(s, workspace_id):
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert await resolve_workspace_model_account(s, run) is None
        await s.commit()
        decision = (await s.execute(select(Decision).where(Decision.run_id == run_id))).scalar_one()
        return decision


async def _notified(sf_: Any, workspace_id: uuid.UUID, decision_id: uuid.UUID) -> bool:
    from backend.notifications.db import NotificationEventRow

    async with sf_() as s, workspace_session_scope(s, workspace_id):
        rows = await s.execute(
            select(NotificationEventRow).where(
                NotificationEventRow.dedupe_key == f"needs_you:{decision_id}"
            )
        )
        return rows.first() is not None


class TestAmbiguousModelAccount:
    async def test_it_asks_which_account_and_offers_them(self, sf) -> None:
        workspace_id, run_id, _ = await _seed(sf, labels=["opus", "sonnet"])

        decision = await _unresolved(sf, workspace_id, run_id)

        assert decision.decision == "ambiguous_model_account"
        assert _question_text(decision, "ko").strip()
        options = _decision_options(decision)
        assert options is not None
        assert any("opus" in o for o in options)
        assert any("sonnet" in o for o in options)

    async def test_the_founder_is_told(self, sf) -> None:
        """30 hours unnoticed — the Decision must reach the notification outbox."""
        workspace_id, run_id, _ = await _seed(sf, labels=["opus", "sonnet"])

        decision = await _unresolved(sf, workspace_id, run_id)

        assert await _notified(sf, workspace_id, decision.id)

    async def test_picking_an_account_makes_it_the_default_and_resumes(self, sf) -> None:
        from backend.workflow.application.checkpoint_resolution import resolve_checkpoint

        workspace_id, run_id, ids = await _seed(sf, labels=["opus", "sonnet"])
        decision = await _unresolved(sf, workspace_id, run_id)
        options = _decision_options(decision)
        assert options is not None
        sonnet = next(o for o in options if "sonnet" in o)

        async with sf() as s, workspace_session_scope(s, workspace_id):
            await resolve_checkpoint(
                s,
                workspace_id=workspace_id,
                checkpoint_id=decision.id,
                answer=sonnet,
                actor_id=uuid.uuid4(),
            )
            await s.commit()

        async with sf() as s, workspace_session_scope(s, workspace_id):
            ws = await s.get(WorkspaceRow, workspace_id)
            run = await s.get(ExecutionRun, run_id)
            assert ws is not None and run is not None
            assert ws.default_account_id == ids[1]
            assert run.status is RunStatus.OPEN


class TestNoModelAccount:
    async def test_it_asks_and_offers_retry_or_discard(self, sf) -> None:
        workspace_id, run_id, _ = await _seed(sf, labels=[])

        decision = await _unresolved(sf, workspace_id, run_id)

        assert decision.decision == "no_model_account"
        assert _question_text(decision, "ko").strip()
        actions = _decision_actions(decision)
        assert actions is not None
        assert {a.key for a in actions} == {"retry", "discard"}
        assert await _notified(sf, workspace_id, decision.id)


# ── The guard: every kind the code can create has a question and a way out ────

_BACKEND = Path(__file__).resolve().parents[2] / "backend"

#: Every Decision kind, with the payload its creation site actually writes (minus
#: anything the founder would see). ``ask_user_question`` carries the agent's own
#: question; ``human_review_required`` has no creator any more but stays mapped.
_KINDS: dict[str, dict[str, Any]] = {
    "ask_user_question": {"question": "Which DB?", "options": ["pg", "sqlite"]},
    "merge_conflict_review": {"reason": "conflict_unresolved_escalated"},
    "verification_failed": {"reason": "round_cap_reached"},
    "run_token_cap_reached": {"reason": "run_token_cap_reached", "token_cap": 1},
    "run_drive_failed": {"reason": "drive_failed_repeatedly"},
    "merge_watch_stalled": {"reason": "ci_failed"},
    "human_review_required": {"reason": "no_verification_declared"},
    "no_model_account": {},
    "ambiguous_model_account": {"options": ["opus", "sonnet"]},
    "product_bundle_conflict": {"reason": "product_bundle_publish_conflict"},
}


@pytest.mark.parametrize("kind", sorted(_KINDS))
@pytest.mark.parametrize("language", ["en", "ko"])
def test_every_kind_asks_something(kind: str, language: str) -> None:
    decision = Decision(decision=kind, payload=_KINDS[kind])
    assert _question_text(decision, language).strip(), f"{kind} has a blank question"


@pytest.mark.parametrize("kind", sorted(k for k in _KINDS if k != "ask_user_question"))
def test_every_system_question_is_translated(kind: str) -> None:
    """A blank or missing Korean line silently falls back to English
    (``_question_text``), so "non-empty in ko" cannot catch it. A system-written
    question must actually DIFFER between the two languages. (``ask_user_question``
    carries the agent's own words, already in the founder's language.)"""
    decision = Decision(decision=kind, payload=_KINDS[kind])
    assert _question_text(decision, "ko") != _question_text(decision, "en"), kind


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_every_kind_offers_a_way_forward(kind: str) -> None:
    decision = Decision(decision=kind, payload=_KINDS[kind])
    assert _decision_actions(decision) or _decision_options(decision), f"{kind} is a dead end"


def _returned_strings(func: ast.AST) -> set[str]:
    return {
        n.value.value
        for n in ast.walk(func)
        if isinstance(n, ast.Return)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
    }


def _created_kinds() -> set[str]:
    """Every kind the backend can create — the ``kind=`` / ``decision=`` argument of
    ``create_decision`` / ``_create_decision`` / ``Decision(``, resolved as:

    * a string literal → itself;
    * a module-level name → its value (imported through the module);
    * the enclosing function's own PARAMETER → a pass-through (the funnel
      ``create_decision`` and the loop's ``_create_decision`` wrapper): the kinds
      are collected at ITS call sites, so it is skipped here;
    * a local assigned from a call to a function in the same module → every
      string literal that function returns (``_ask_decision_kind``);
    * anything else → ``<unresolved NAME>``, which fails the guard on purpose.
    """
    import importlib

    found: set[str] = set()
    key = {"create_decision": "kind", "_create_decision": "kind", "Decision": "decision"}
    for path in sorted(_BACKEND.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module: Any = None
        local_funcs = {
            f.name: f
            for f in ast.walk(tree)
            if isinstance(f, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for fn in local_funcs.values():
            params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            assigns = {
                t.id: n.value
                for n in ast.walk(fn)
                if isinstance(n, ast.Assign)
                for t in n.targets
                if isinstance(t, ast.Name)
            }
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                arg = key.get(name or "")
                kw = next((k for k in node.keywords if k.arg == arg), None) if arg else None
                if kw is None:
                    continue
                value = kw.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(value.value)
                elif isinstance(value, ast.Name) and value.id in params:
                    continue  # pass-through: collected at the caller
                elif isinstance(value, ast.Name) and value.id in assigns:
                    source = assigns[value.id]
                    callee = getattr(getattr(source, "func", None), "id", None)
                    if isinstance(source, ast.Call) and callee in local_funcs:
                        found |= _returned_strings(local_funcs[callee]) or {
                            f"<unresolved {value.id}>"
                        }
                    else:
                        found.add(f"<unresolved {value.id}>")
                elif isinstance(value, ast.Name):
                    if module is None:
                        rel = path.relative_to(_BACKEND.parent).with_suffix("")
                        module = importlib.import_module(".".join(rel.parts))
                    resolved = getattr(module, value.id, None)
                    found.add(resolved if isinstance(resolved, str) else f"<unresolved {value.id}>")
                else:
                    found.add(f"<unresolved {ast.unparse(value)}>")
    return found


def test_the_scan_sees_the_kinds_it_must() -> None:
    """Control — the scan is only a guard if it actually finds the kinds that exist."""
    kinds = _created_kinds()
    for expected in (
        "ask_user_question",
        "merge_conflict_review",
        "run_token_cap_reached",
        "run_drive_failed",
        "no_model_account",
        "ambiguous_model_account",
    ):
        assert expected in kinds, expected


def test_the_guard_covers_every_kind_the_code_creates() -> None:
    """A new kind must come with its question and actions — or this goes red."""
    assert _created_kinds() <= set(_KINDS), _created_kinds() - set(_KINDS)


async def test_a_lookup_that_only_needs_the_account_writes_no_decision(sf) -> None:
    """The merge watch resolves an account only to find which worker holds a
    checkout. It must not stop the run on a Decision — it was only harmless because
    that session happened never to commit (#1074 inventory)."""
    from backend.workflow.application.runtime.account_resolution import (
        resolve_workspace_model_account,
    )

    workspace_id, run_id, _ = await _seed(sf, labels=["opus", "sonnet"])

    async with sf() as s, workspace_session_scope(s, workspace_id):
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert await resolve_workspace_model_account(s, run, record_decision=False) is None
        await s.commit()
        rows = await s.execute(select(Decision).where(Decision.run_id == run_id))
        assert rows.first() is None


def test_the_merge_watch_lookup_asks_for_no_decision() -> None:
    """The call site, not just the flag: the merge watch's resolve passes it."""
    path = _BACKEND / "workflow" / "application" / "runtime" / "merge_watch_client_box.py"
    calls = [
        n
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "resolve_workspace_model_account"
    ]
    assert calls, "the lookup moved — re-point this check"
    for call in calls:
        kw = next((k for k in call.keywords if k.arg == "record_decision"), None)
        assert kw is not None and isinstance(kw.value, ast.Constant) and kw.value.value is False


async def test_a_bundle_publish_conflict_is_a_report_on_a_shipped_run(sf) -> None:
    """The merge to main already succeeded — only publishing the durable copy hit a
    divergence. It used to be a ``merge_conflict_review`` offering retry/discard on a
    run that ships right after, which the transition table now refuses silently. It
    is a report: acknowledge, and a reply never re-drives the run (#1074 inventory)."""
    from backend.workflow.application._checkpoint_shared import ACTION_ACKNOWLEDGE
    from backend.workflow.application.agent_runner import AgentRunner
    from backend.workflow.application.checkpoint_resolution import _REPORT_DECISION_KINDS

    workspace_id, run_id, _ = await _seed(sf, labels=["only"])

    async with sf() as s, workspace_session_scope(s, workspace_id):
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        await AgentRunner(s)._raise_bundle_conflict_decision(run, ["a.py"])
        await s.commit()
        decision = (await s.execute(select(Decision).where(Decision.run_id == run_id))).scalar_one()

    assert decision.decision == "product_bundle_conflict"
    actions = _decision_actions(decision)
    assert actions is not None and {a.key for a in actions} == {ACTION_ACKNOWLEDGE}
    assert decision.decision in _REPORT_DECISION_KINDS
