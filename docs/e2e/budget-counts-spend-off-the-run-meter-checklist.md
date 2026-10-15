# The workspace budget counts spend off the run meter

`sum_workspace_tokens_since` summed `execution_runs.usage_*` only. The workspace ledger
(`unattributed_llm_usage`, #954) was written and read by nothing, and late executor reports
(#928, #1158) stayed on the task row. Now a late report also books a ledger row
(`site="executor_late_report"`, identity `task_id` + `run_id`) in the same transaction, and the
budget sums runs + ledger.

Backend only — autodeployed by the container poller. No migration.

## Unit / wire (local)

- [x] run-less spend pushes a workspace over its budget → `/api/v1/messages` 429
- [x] ledger spend before the window does not count (control)
- [x] another workspace's ledger is not this one's spend (read across tenants, so the query's own predicate is what is under test)
- [x] a late report on `/workers/result` → the founder's next submission is refused
- [x] a late report books ONE ledger row with its run; a replay books none
- [x] an on-time report books no ledger row (no double count with the run meter)
- [x] wire cuts (sum · window · workspace predicate · hook call · API hook) each red — on SQLite AND on a throwaway PG with RLS (`bsvibe_app` role, `alembic upgrade head`)

## Prod (after deploy)

- [ ] 형님 DB read, before vs after: for the founder workspace this month,
      `SELECT sum(usage_prompt_tokens+usage_completion_tokens) FROM unattributed_llm_usage WHERE workspace_id=… AND created_at >= date_trunc('month', now())`
      — that is how much the budget grew by. Compare to `monthly_token_budget` before anything gets refused
- [ ] After #1161's worker restart, cancel a running Claude Code run mid-turn → backend log
      `executor_task_late_usage_recorded`, and a `unattributed_llm_usage` row with `site='executor_late_report'` for that task

## ⚠️ Before deploying

The founder workspace's ingest/bootstrap spend now counts. If this month's ledger is large, the
first submission after deploy could be refused (`workspace_token_budget_reached`). Read the sum
above first.
