# E2E — wire `abort_merge` (audit B4)

Closes the one audit path that can corrupt product `main`: a verify-time merge
conflict left the run worktree mid-merge (`MERGE_HEAD` + `<<<<<<<` markers on
disk), and the run's next round `commit_worktree` (`git add -A`) committed those
markers onto `bsvibe/run/<rid>` — from where they reach `main` via
`merge_to_main` / `force_merge_theirs`.

## Wiring points (evidence-chosen)

| Point | Wired? | Why |
|---|---|---|
| `verification_service.verify` — conflict branch | ✅ abort | The live corruption path. After recording FAILED, abort so the tree is clean; next round re-merges from a clean tree. |
| `run_cleanup.cancel_run` | ✅ abort (best-effort) | Cancel keeps the worktree on disk — a run cancelled mid-merge would keep markers. Non-redundant. |
| `run_cleanup.discard_run` | ❌ (redundant) | `remove_run_worktree --force` deletes the whole worktree dir + branch; markers vanish with it. No abort needed. |

Idempotency confirmed: `git merge --abort` on a clean tree exits 128 ("no merge
to abort"), which `abort_merge` swallows via `check=False` — safe to call on the
clean path.

## Automated coverage (real git ops, no stubs)

- [x] `tests/glue/test_verify_merge_autoship.py::test_verify_merge_conflict_aborts_so_next_commit_is_clean`
      — drives real `verify()`; asserts (a) no `MERGE_HEAD`, (b) no markers on
      disk, (c) a subsequent `commit_worktree` branch tip carries no `<<<<<<<`.
- [x] `...::test_verify_surfaces_merge_conflict_as_failed` — FAILED + conflict
      paths still surfaced (fix cleans the tree, does not hide the conflict).
- [x] `tests/workflow/application/test_run_cleanup.py::test_cancel_aborts_mid_merge_worktree`
      — induces a mid-merge worktree, cancels the run, asserts the worktree
      survives but is no longer mid-merge and has no markers.
- [x] `tests/storage/test_product_workspace_merge.py::test_merge_main_conflict_surfaces_paths_and_leaves_markers`
      — unchanged: `merge_main_into_worktree` in isolation still leaves markers
      (abort is the caller's job).

## Manual / live smoke (product run)

- [ ] Two parallel runs on one product; run A ships a change to `main` that run B
      also touched. Run B's verify hits `merge_conflict` → FAILED.
- [ ] Inspect run B's worktree: `git -C var/runs/<B> rev-parse -q --verify MERGE_HEAD`
      returns nonzero (no merge in progress); no `<<<<<<<` in the conflicted file.
- [ ] Let run B take another round; confirm its next commit on `bsvibe/run/<B>`
      contains no conflict markers, and `main` never receives a `<<<<<<<` blob.
- [ ] Cancel a run via `bsvibe_runs_cancel` while it is mid-merge; confirm the
      worktree is left clean.

## CI gate (all green)

- `uv run pytest --cov=backend --cov=plugin --cov-fail-under=80 -q` → 5053 passed,
  18 skipped, coverage **89.29%**.
- `uv run pytest bsvibe_sdk/tests/ -q` → 24 passed.
- `uv run mypy --strict backend/` → clean.
- `uv run ruff check backend/ tests/` → clean.
- `uv run ruff format --check backend/ tests/` → clean.
- `uv run lint-imports` → 5 kept, 0 broken.
