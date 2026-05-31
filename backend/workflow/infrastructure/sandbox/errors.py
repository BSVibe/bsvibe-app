"""Sandbox error types."""

from __future__ import annotations


class SandboxError(Exception):
    """A sandbox operation failed or was refused (path escape, bad
    command, IO error). The dispatcher surfaces the message back to the
    work LLM as the tool result so it can recover."""


class SandboxUnavailable(SandboxError):
    """The sandbox backend (the DinD daemon) could not be reached.

    Distinct from ``SandboxError`` so callers can degrade — a missing
    backend must never become a false ``verified``/``failed``."""
