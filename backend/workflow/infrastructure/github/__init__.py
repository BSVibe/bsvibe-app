"""GitHub auto-merge durable state (PR3).

Holds the ``github_merge_watch`` table — one row per opened PR eligible for
auto-merge — plus its repository + claim statement. A later CI-green
auto-merge poller worker drains this table; there is no worker yet.
"""

__all__: list[str] = []
