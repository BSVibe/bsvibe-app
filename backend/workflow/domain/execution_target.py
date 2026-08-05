"""Execution target — WHERE a run's coding agent runs (#692).

A product declares its execution target in its free-form ``metadata`` object
(the founder's open product-metadata design — no rigid enum column) under the
``execution_target`` key. Two values today:

* ``server_sandbox`` (default) — the current model: the agent's file/shell work
  routes through the MCP work tools into the run's server-side worktree + DinD
  sandbox. Strong isolation; secrets need server-side injection (#691).
* ``client_attach`` — the agent runs natively in the user's OWN working
  directory on a registered worker host, continuing the user's work in place
  (Claude Code local). Files / env / toolchain are whatever is already there;
  the user's machine is trusted. See ``~/Docs/BSVibe_Client_Attach_Execution_Design.md``.

This module is the SINGLE reader + validator so dispatch, the worker, and the
read models never drift on the vocabulary or the default.
"""

from __future__ import annotations

from typing import Any

#: The metadata key a product uses to declare its execution target.
EXECUTION_TARGET_KEY = "execution_target"

#: The metadata key holding the absolute path, on the client worker's host, of
#: the user's own working directory a ``client_attach`` product runs in.
CLIENT_WORKSPACE_PATH_KEY = "client_workspace_path"

SERVER_SANDBOX = "server_sandbox"
CLIENT_ATTACH = "client_attach"

#: The default when a product says nothing: the isolated, server-side model. An
#: unrecognised value degrades to THIS — a typo must never silently enable
#: execution on a user's machine.
DEFAULT_EXECUTION_TARGET = SERVER_SANDBOX

VALID_EXECUTION_TARGETS = frozenset({SERVER_SANDBOX, CLIENT_ATTACH})


def read_execution_target(product_metadata: Any) -> str:
    """Resolve a product's execution target from its ``metadata``.

    Tolerant by design: a missing key, non-dict metadata, or any unknown /
    non-string value falls back to :data:`DEFAULT_EXECUTION_TARGET`
    (``server_sandbox``). Never raises — a hand-edited metadata blob cannot break
    dispatch — and never upgrades an unrecognised value into client execution."""
    if not isinstance(product_metadata, dict):
        return DEFAULT_EXECUTION_TARGET
    value = product_metadata.get(EXECUTION_TARGET_KEY)
    if isinstance(value, str) and value in VALID_EXECUTION_TARGETS:
        return value
    return DEFAULT_EXECUTION_TARGET


def is_client_attach(product_metadata: Any) -> bool:
    """True iff the product declares (validly) the ``client_attach`` target."""
    return read_execution_target(product_metadata) == CLIENT_ATTACH


def read_client_workspace_dir(product_metadata: Any) -> str | None:
    """The user's working directory (worker-host path) for a ``client_attach``
    product, or ``None``.

    Returns the path ONLY when the product is validly ``client_attach`` AND a
    non-blank string path is declared. A path set under the (default)
    ``server_sandbox`` model is ignored — a stray local path must never leak
    into the server sandbox's cwd. Never raises."""
    if not is_client_attach(product_metadata):
        return None
    # is_client_attach already proved metadata is a dict.
    value = product_metadata.get(CLIENT_WORKSPACE_PATH_KEY)
    if isinstance(value, str) and value.strip():
        return value
    return None


__all__ = [
    "CLIENT_ATTACH",
    "CLIENT_WORKSPACE_PATH_KEY",
    "DEFAULT_EXECUTION_TARGET",
    "EXECUTION_TARGET_KEY",
    "SERVER_SANDBOX",
    "VALID_EXECUTION_TARGETS",
    "is_client_attach",
    "read_client_workspace_dir",
    "read_execution_target",
]
