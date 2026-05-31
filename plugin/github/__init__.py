"""GitHub connector plugin (Workflow §6 #4 capability model).

Capabilities:

* ``@p.inbound`` — parse a GitHub webhook (issue / PR / comment) into a
  :class:`backend.intake.schema.TriggerEvent`, with HMAC-SHA256 signature
  verification and ``X-GitHub-Delivery`` as the idempotency key.
* ``@p.outbound(artifact_types=["code", "pr"])`` — open / update a PR.
* ``@p.outbound(artifact_types=["issue_comment"])`` — post a comment.
* ``@p.compensate`` — close a PR (T2, trail) / delete a comment (T1, clean),
  both idempotent.
* ``@p.action`` — ``open_pr`` / ``comment`` exposed as agent-loop tools.
* ``@p.setup`` — token / webhook-secret credential flow.

All external I/O goes through :class:`~.client.GithubClient` (httpx); tests
mock httpx and never reach real GitHub.
"""

from __future__ import annotations
