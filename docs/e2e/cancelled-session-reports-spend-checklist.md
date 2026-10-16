# #1158 — a cancelled executor session reports what it spent

Worker-only change. **Host workers are NOT autodeployed** — after merge:
`git pull` on `main` in the worker checkout, then
`launchctl kickstart -k gui/501/com.bsvibe.worker-mac-mini-e2e` and confirm the **process start
time** moved. The backend needs nothing new (#928's usage-on-`failed` path takes the report).

## Unit / wire (local)

- [x] cancel mid-session → one `failed` result with the in-session tally (weighted + raw split)
- [x] cancel before any usage → no POST (Lift E14 rule kept)
- [x] worker deadline → the `failed` result carries the tally
- [x] a refused report does not swallow the cancel
- [x] the posted body parses under `WorkerResultBody` (`extra="forbid"`)
- [x] wire cuts: executor tally hand-off · `_publish` · context key · cancel report · deadline usage — each red, 5 collected each

## Prod (after the worker restart)

- [ ] Cancel a run mid-turn (`bsvibe_runs_cancel` on a Claude Code run that has run a few
      turns). Worker log: `task_cancelled_by_backend` then `task_cancelled_usage_reported` with
      non-zero `usage_prompt_tokens`
- [ ] Backend log (`docker --context colima logs bsvibe-prod-backend-1`):
      `executor_task_late_usage_recorded` for the same `task_id` — NOT `executor_result_not_dispatched`
- [ ] 형님 DB read: that `executor_tasks` row is `failed`, its `output` unchanged, usage columns
      non-zero
- [ ] Control: a run cancelled before its first assistant message → no `task_cancelled_usage_reported`

## Not covered on purpose

- The run meter (`execution_runs.usage_*`) does NOT see this number — it lands on the task row,
  like #928's late reports; folding it into the run would race the drive loop's absolute ORM
  write. The workspace budget DOES see it since #1162, through the ledger row
  (`site="executor_late_report"`).
- codex / opencode keep no in-session tally; their cancelled sessions still report nothing.
