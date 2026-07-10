# E2E Checklist — Retract lazy-apply + dedupe

Fixes: queued retract never wrote its tombstone (no apply trigger existed); read
tools surfaced tombstoned notes; repeated retracts minted duplicate signals.

## Apply pipeline (the core bug)

- [x] A queued retract past its 30s window commits its `retracted_at` frontmatter
      on the next garden read — verified `test_list_recent_lazy_applies_tombstone_and_hides_note`
      (tombstone bytes present on disk after `list_recent`).
- [x] The tombstone is a frontmatter marker, not a delete — file still exists after apply.
- [x] `apply_pending` no longer wedges on a since-deleted note — the row is marked
      applied and no error escapes — `test_apply_pending_missing_note_marks_applied_without_raising`.
- [x] A read never 500s on a bad sweep row — `_resolve_pending_retractions` swallows + logs.

## Read surface hides tombstoned notes

- [x] `list_recent` omits a retracted note — `test_list_recent_lazy_applies_tombstone_and_hides_note`.
- [x] `search` omits a retracted note — `test_search_hides_retracted_note`.
- [x] `get_note` returns "not found" for a retracted note — `test_get_note_hides_retracted_note`.
- [x] `list_tags` skips retracted notes (predicate shared across all four read tools).
- [x] `limit` is applied *after* the retracted-skip so a founder still gets N live notes.

## Dedupe on node_ref (no duplicate signals)

- [x] Second retract on the same node (no `correction_id`) returns the existing
      pending signal + `outcome="already_pending"`, no new row —
      `test_issue_dedupes_pending_retract_on_node_ref`.
- [x] Retract on an already-applied node returns `outcome="already_applied"` —
      `test_issue_after_applied_returns_already_applied`.
- [x] A cancelled (undone) retract does NOT block a fresh retract —
      `test_issue_after_undo_allows_new_retract`.
- [x] `correct` is not deduped (founder may correct a node repeatedly).
- [x] REST `RetractResponse` and the MCP envelope both carry the `outcome` field.

## Regression guard

- [x] `undo` after the window still returns `expired` when nothing applied it in the
      meantime; returns `already_applied` once a read has committed the tombstone.
- [x] Full suites green: tests/mcp, tests/knowledge, tests/api inside+deliverables retract (1185 passed).
- [x] `lint-imports` green (added the parallel `knowledge_tools -> retraction_service` exemption).
- [x] `ruff check` + `ruff format --check` clean on all touched files.
