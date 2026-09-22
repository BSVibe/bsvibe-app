"""Where a deliverable actually goes — account config + this binding's override.

Split out of ``_resolver`` when that file crossed the package's 400-LOC cap
(Lift §17.7). The cap named the right home: destination resolution is its own
question, and keeping it here lets the rule be read without the 300 lines of
binding-qualification around it.

The binding is taken structurally (:class:`_DeliveryBinding`) so this module does
not import ``_resolver`` — ``_resolver`` imports it, and a cycle would break the
package's "every helper module is importable on its own" guard.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

import structlog

from backend.connectors.db import ConnectorAccountRow

logger = structlog.get_logger(__name__)


class _DeliveryBinding(Protocol):
    """The two fields destination resolution reads off a resolved binding."""

    account: ConnectorAccountRow
    selection: dict[str, Any]


def effective_delivery_config(binding: _DeliveryBinding) -> dict[str, Any]:
    """The delivery target for this product × account — #1003.

    ``delivery_config`` belongs to the ACCOUNT, so every product bound to one
    account shipped to the same place; splitting two products across two chats
    meant standing up a second connector account (a second bot token) purely to
    hold a second ``chat_id``.

    The per-binding slot already existed and was already populated — measured on
    prod 2026-09-22, all three live bindings carried a ``selection`` equal to
    their account's ``delivery_config`` — but :func:`_resolve_bindings` selected
    only ``connector_account_id``, so it never reached the dispatch loop. Because
    every live row already agrees, honouring it is a **no-op on today's data**;
    it starts mattering the first time someone sets a different one.

    Precedence is account-then-binding: the narrower statement wins, and an empty
    ``selection`` (the default, and the overwhelming majority of rows) changes
    nothing.

    ⚠️ Call this — do not re-spell the merge. The dispatch loop feeds this config
    to the event builder AND to the plugin context, and merging at one but not
    the other would aim the event at the new room while the plugin still ran
    against the old config.
    """
    return {**(binding.account.delivery_config or {}), **binding.selection}


def _selection_by_account(
    bound_rows: list[tuple[uuid.UUID, dict[str, Any]]],
    *,
    workspace_id: uuid.UUID,
) -> dict[uuid.UUID, dict[str, Any]]:
    """One delivery override per account — or none, when the rows disagree.

    ``resource_bindings`` carries NO uniqueness on (product, account): two rows
    for one pair are legal and can name two different rooms. Taking either one
    would make an outward-facing destination depend on row order, so a
    disagreement DROPS the override and the account's own configured target
    stands — the behaviour that was already documented.

    Duplication alone is not ambiguity: rows that agree still override, or the
    guard would refuse whenever a pair simply has two rows.
    """
    grouped: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for account_id, selection in bound_rows:
        if selection:
            grouped.setdefault(account_id, []).append(selection)
    resolved: dict[uuid.UUID, dict[str, Any]] = {}
    for account_id, selections in grouped.items():
        distinct = {json.dumps(s, sort_keys=True, default=str) for s in selections}
        if len(distinct) > 1:
            logger.warning(
                "connector_delivery_ambiguous_binding_selection",
                connector_account_id=str(account_id),
                workspace_id=str(workspace_id),
                candidates=sorted(distinct),
            )
            continue
        resolved[account_id] = selections[0]
    return resolved


__all__ = ["effective_delivery_config"]
