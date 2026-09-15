"""Workspace request-scoping — defense layer 2 (Workflow §3).

A per-request :data:`current_workspace_id` contextvar drives a global
SQLAlchemy ``do_orm_execute`` listener that injects
``with_loader_criteria(workspace_id == current)`` into every ORM SELECT.
Any mapped class that declares a ``workspace_id`` column is auto-scoped;
a class may opt out with ``__exclude_workspace_filter__ = True`` (the
``memberships`` table does, since it is what resolution reads *from*).

Layering note
-------------
The criteria is built as a concrete Core expression (``cls.workspace_id ==
ws``) rather than a lambda. ``with_loader_criteria`` caches lambda closures,
so a lambda closing over the per-request ``ws`` would freeze the first
request's value into the compiled-statement cache. A freshly-built
expression sidesteps that trap entirely.

When the contextvar is unset (``None``) the listener is a no-op — code paths
that never establish a workspace (auth resolution itself, the workspaces
router, every pre-existing test) behave exactly as before.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from backend.data import Base

current_workspace_id: ContextVar[uuid.UUID | None] = ContextVar(
    "current_workspace_id", default=None
)


def set_current_workspace_id(workspace_id: uuid.UUID) -> Token[uuid.UUID | None]:
    """Bind the active workspace for the current context; returns a reset token."""
    return current_workspace_id.set(workspace_id)


def reset_current_workspace_id(token: Token[uuid.UUID | None]) -> None:
    """Restore the contextvar to its prior value (use the ``set`` token)."""
    current_workspace_id.reset(token)


@contextmanager
def workspace_scope(workspace_id: uuid.UUID) -> Iterator[None]:
    """Bind ``workspace_id`` for the duration of the block, then restore.

    The publication point for code that has no request to hang scoping off —
    the background paths (#959). A queue poller claims work ACROSS tenants by
    design, but everything it does *after* the claim belongs to exactly one
    workspace; wrapping that stretch here engages layer 2 (this contextvar's
    ORM auto-filter) and, through the ``after_begin`` listener in
    :mod:`backend.data.rls`, layer 3 (the Postgres RLS GUC) for every
    transaction opened inside it.

    Restores on the way out INCLUDING on an exception — a crashed drive must
    not leave the next claim scoped to the workspace that crashed.

    The ``backend.data.rls`` import is deliberate and load-bearing (it is here
    rather than at module level only because rls imports THIS module): layer 3's
    listener registers on that module's import, and the worker process's import
    graph was measured NOT to reach it. Without this line a background path
    would get layer 2 and silently no layer 3 — the one process #959 is about.
    """
    from backend.data import rls  # noqa: PLC0415, F401 — installs the layer-3 listener

    token = set_current_workspace_id(workspace_id)
    try:
        yield
    finally:
        reset_current_workspace_id(token)


def _scoped_mappers() -> list[Any]:
    """Mapped classes that carry a ``workspace_id`` column and don't opt out.

    Recomputed each call so models registered after import (e.g. in tests)
    are seen. The set is tiny (~a dozen tables), so this is cheap.
    """
    out: list[Any] = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if getattr(cls, "__exclude_workspace_filter__", False):
            continue
        if "workspace_id" in mapper.columns:
            out.append(cls)
    return out


def _add_workspace_criteria(state: ORMExecuteState) -> None:
    if not state.is_select or state.is_column_load or state.is_relationship_load:
        return
    ws = current_workspace_id.get()
    if ws is None:
        return
    for cls in _scoped_mappers():
        state.statement = state.statement.options(
            with_loader_criteria(
                cls,
                cls.workspace_id == ws,  # concrete expr — not a cached lambda
                include_aliases=True,
            )
        )


_installed = False


def install_workspace_filter() -> None:
    """Register the ``do_orm_execute`` listener once, process-wide."""
    global _installed  # noqa: PLW0603 — install-once guard for the process-wide listener
    if _installed:
        return
    event.listen(Session, "do_orm_execute", _add_workspace_criteria)
    _installed = True


# Register on import so any Session — app or test — is scoped without extra
# wiring. Idempotent via the module-level guard.
install_workspace_filter()
