"""#692 — the execution-target vocabulary reader (WHERE a run's agent runs).

A product declares its execution target in its free-form ``metadata`` under the
``execution_target`` key. This is the single reader + validator so dispatch, the
worker, and the read models never drift on the vocabulary or the default. The
safety property: anything unrecognised falls back to ``server_sandbox`` (the
isolated default) — an unknown / typo'd value must NEVER silently enable
client-side execution on a user's machine.
"""

from __future__ import annotations

from backend.workflow.domain.execution_target import (
    CLIENT_ATTACH,
    DEFAULT_EXECUTION_TARGET,
    SERVER_SANDBOX,
    is_client_attach,
    read_execution_target,
)


def test_default_is_server_sandbox() -> None:
    assert DEFAULT_EXECUTION_TARGET == SERVER_SANDBOX


def test_missing_key_falls_back_to_default() -> None:
    assert read_execution_target({}) == SERVER_SANDBOX
    assert read_execution_target({"other": "x"}) == SERVER_SANDBOX


def test_non_dict_metadata_falls_back_to_default() -> None:
    assert read_execution_target(None) == SERVER_SANDBOX
    assert read_execution_target("client_attach") == SERVER_SANDBOX
    assert read_execution_target(["client_attach"]) == SERVER_SANDBOX


def test_valid_values_pass_through() -> None:
    assert read_execution_target({"execution_target": "server_sandbox"}) == SERVER_SANDBOX
    assert read_execution_target({"execution_target": "client_attach"}) == CLIENT_ATTACH


def test_unknown_value_degrades_to_safe_default_not_client() -> None:
    # A typo ('client-attach') or any unknown string must NOT enable client
    # execution — it degrades to the isolated server default.
    assert read_execution_target({"execution_target": "client-attach"}) == SERVER_SANDBOX
    assert read_execution_target({"execution_target": "local"}) == SERVER_SANDBOX
    assert read_execution_target({"execution_target": ""}) == SERVER_SANDBOX
    assert read_execution_target({"execution_target": True}) == SERVER_SANDBOX


def test_is_client_attach_helper() -> None:
    assert is_client_attach({"execution_target": "client_attach"}) is True
    assert is_client_attach({}) is False
    assert is_client_attach({"execution_target": "client-attach"}) is False
