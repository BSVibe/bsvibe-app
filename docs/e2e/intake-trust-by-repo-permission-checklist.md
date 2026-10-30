# Intake trust by repo permission — E2E checklist

An issue edit or PR comment from someone GitHub reports as `CONTRIBUTOR` (a private org
member) is routed to the run when that person has write access to the repo.

## Automated (`tests/workflow/application/test_intake_trust_by_repo_permission.py`)

- [x] OWNER / MEMBER / COLLABORATOR are trusted without a GitHub call
- [x] CONTRIBUTOR with `admin` / `maintain` / `write` is trusted; `read` / `triage` / `none` / unknown are not
- [x] A failed lookup, or no lookup wired, trusts only the association
- [x] Issue edit and PR comment routes both use the lookup
- [x] The real `IntakeWorker.drain_once` routes a CONTRIBUTOR founder's edit and comment to the run (no new request)
- [x] `build_worker_runtime` gives the intake worker a lookup
- [x] `GithubClient.repo_permission` reads `role_name` (keeps `maintain`) and maps 404 to `None`
- [x] The production lookup asks about the event's repo with the binding's token; no binding → `None`
- [x] Wire cuts: each edge (worker → both routes, runtime wiring, both routes' lookup, `role_name`, write set) turns a test red

## Live (after deploy)

- [ ] Edit the body of an issue whose run is in flight, as the founder → worker log shows
      `issue_edit_routed_to_run`, not `issue_edit_not_routed_untrusted_editor`, and no new request
- [ ] A comment by an account without write access is still logged `*_not_routed_*`
