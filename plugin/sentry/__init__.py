"""Sentry connector plugin (Workflow §6 #4 capability model).

Capabilities:

* ``@p.inbound`` — parse a Sentry webhook (``issue`` / ``event_alert``
  resources) into a :class:`backend.workflow.domain.incoming.TriggerEvent`, with
  HMAC-SHA256 signature verification (``Sentry-Hook-Signature``, bare hex) and
  a stable idempotency key derived from the Sentry hook / issue / event id.
* ``@p.outbound(artifact_types=["sentry_issue_update"])`` — resolve an issue
  (``PUT /issues/{id}/`` with ``status:resolved``).
* ``@p.compensate`` — re-open the issue (``status:unresolved``) — T2 (trail:
  Sentry records the resolve/unresolve in the issue activity), idempotent.
* ``@p.action`` — ``resolve_issue`` exposed as an agent-loop tool.
* ``@p.setup`` — auth-token / client-secret credential flow.

All external I/O goes through :class:`~.client.SentryClient` (httpx); tests
mock httpx and never reach real Sentry.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
