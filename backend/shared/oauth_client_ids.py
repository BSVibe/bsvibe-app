"""Cross-context wire-contract vocabulary — OAuth ``client_id`` literals (neutral shared-kernel leaf).

A ``client_id`` on the RFC 8628 device grant is a *wire contract*: the CLI
(Executors) puts it on the form at ``/device_authorization``, and the
authorization server (Identity) reads it back out when the consent screen asks
"who is this?". Two contexts, one string — so its single definition lives here,
in the shared kernel, rather than in either of them. The alternative is what it
replaced: the server importing the CLI's login module, which drags the whole
``bsvibe`` command-line surface (``click`` and ~320 modules) into the
production API import path and points the dependency backwards — the CLI
depends on the server's answer, never the reverse.

This module imports nothing from any bounded context, so it satisfies the
"common leaves do not import bounded contexts" import-linter contract that
guards ``backend.shared``.
"""

from __future__ import annotations

#: ``bsvibe-cli`` — the client identifier ``bsvibe login`` presents on the
#: device grant. That grant is a PUBLIC-client flow with no secret, and its
#: security rests on the human approving a short code rather than on client
#: identity, so there is deliberately no dynamic-registration step: prod
#: carries no ``oauth_clients`` row for this id even though it is behind most
#: device sign-ins. Identity's first-party allow-list is therefore the ONLY
#: thing that can tell the real CLI from a string an attacker typed, which is
#: why this literal must have exactly one definition site.
DEVICE_CLIENT_ID = "bsvibe-cli"

__all__ = [
    "DEVICE_CLIENT_ID",
]
