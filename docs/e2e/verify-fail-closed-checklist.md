# E2E Checklist — B1: verification fails CLOSED when its objective gate cannot run

Governing spec: `docs/architecture/INVARIANTS.md` INV-2 (fail-closed 3-state).
Scope: `backend/workflow/application/verification_service.py` (the derived-gate
fold + `gate_expected`). Non-web change — verified by driving the real
`VerificationService.verify` path (no browser).

The defect closed: a transient deriver failure (`except Exception: return None`)
conflated "deriver crashed" with "repo has no toolchain", disabling the grade-D
founder-review ratchet and letting a run reach PROVED with ZERO gate commands run.

## The four directions (all driven through the real gate logic, not a recording stub)

- [x] **Deriver CRASH on a manifest-present repo → fails CLOSED (not PROVED).**
  A transient deriver failure (provider 500 / timeout) on a repo whose worktree
  has `pyproject.toml`. Even though the agent's own command attestation passes,
  the verification outcome is **FAILED** — the run cannot auto-prove with zero
  objective gate commands executed. `gate_expected=True`, `gate_deriver_failed`
  telemetry recorded, `honesty_grade=None`.
  → `TestGateFailClosed::test_deriver_crash_on_manifest_repo_fails_closed`
  (RED on the pre-fix `command_gate_pass = … else all_cmd_pass`: observed PASSED).

- [x] **LLM self-declares `applicable: false` on a manifest-present repo → does NOT auto-prove.**
  Deterministic manifest presence wins over the LLM's word: the run stays
  `gate_expected=True` + grade **D**, so the ratchet routes it to founder review
  instead of silently auto-proving (the second fail-open door).
  → `TestGateFailClosed::test_llm_not_applicable_cannot_override_present_manifest`

- [x] **Zero objective gate commands on a manifest-present repo → not a vacuous pass.**
  A passing agent-command attestation (`all([])`-style vacuity included) does not
  rescue a crashed gate on a gate-expected repo — proven by the crash test above,
  where the agent command `true` passes yet the outcome is FAILED.
  → `TestGateFailClosed::test_deriver_crash_on_manifest_repo_fails_closed`

- [x] **Manifest-ABSENT (greenfield/prose) worktree → STILL passes (no regression).**
  No manifest → genuinely gateless → a deriver hiccup falls back to the agent's
  command attestation and the run PASSES with `gate_expected=False` (auto-proceed,
  weak grade surfaced but not nagged).
  → `TestGateFailClosed::test_deriver_crash_on_greenfield_still_passes`

## Determinism of `applicable`

- [x] `_manifest_present` is a real file check on the run's server-side worktree
  (`run_worktree_path(run.id)`), over the same `_MANIFEST_FILES` the deriver
  grounds on — manifest present → True, empty repo → False.
  → `TestGateFailClosed::test_manifest_present_is_deterministic`

## Non-regression of the established gate behavior

- [x] Healthy deriver on a normal repo behaves exactly as before — B/A grades,
  real gate failure still FAILS (Q-2), unavailable (127) never false-fails,
  applicable-but-empty on a real project → PASSED + D + review.
  → `TestHonestyGrade`, `TestDerivedGate`, `TestVerdictWiring` (all green).

- [x] The 3-state (`DerivedGateOk | DerivedGateNotEligible | DerivedGateFailed`)
  replaces the `dict | None`; the persisted `result["derived_gate"]` shape is
  unchanged (the blob rides `DerivedGateOk.blob`).
  → `TestDerivedGate` (unwrap assertions).

## Downstream ratchet (unchanged consumers, verified by existing tests)

- [x] FAILED verify → founder Decision, never PROVED
  (`test_native_failing_contract_writes_failed_verification_no_deliverable`).
- [x] PASSED + grade D + `gate_expected` → founder review, not PROVED
  (`test_native_grade_d_with_expected_gate_routes_to_review`).
- [x] Greenfield grade D + `gate_expected=False` → auto-verifies
  (`test_native_grade_d_greenfield_no_gate_expected_auto_verifies`).
