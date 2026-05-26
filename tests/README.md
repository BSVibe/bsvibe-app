# Backend test conventions

This directory holds the FastAPI backend's pytest suites. CI runs the full set
on Postgres; local runs default to in-memory SQLite (see `tests/_support.py`).

## The delta-assertion rule (B13)

**Post-state alone is NOT acceptable.** Each test for an executor / run /
delivery / safe-mode / verify path MUST assert the CHANGE the lift was
supposed to produce — never just a status transition or a row's existence.

The audit (`~/Docs/BSVibe_Feature_Reality_Audit_2026-05-26.md`, RC-5) found
months of green tests on a hollow executor because assertions like
"run reached `REVIEW_READY`" or "a `Deliverable` row exists" silently
accepted output-less, verification-less, knowledge-less runs. The fix is
delta assertions on the four critical dimensions:

| Dimension                  | Assert this (not just status)                                                      |
| -------------------------- | ---------------------------------------------------------------------------------- |
| 1. File round-trip         | `artifact_refs` non-empty AND the captured files are byte-equal via the GET artifact endpoint (see `tests/glue/test_executor_run_e2e.py::test_executor_run_captures_artifact_and_serves_via_endpoint`). |
| 2. Real verification ran   | A `VerificationResult` row exists for the run with the expected `outcome` AND `proof_state` reflects it (PROVED only on PASSED). See `tests/execution/test_proved_invariant.py`. |
| 3. Knowledge consulted     | The retriever was queried (recorded stubs) AND the canon text surfaces in BOTH the B6 first-turn messages AND the persisted `VerificationResult.contract` (see `tests/glue/test_b13_decision_reuse_in_run.py`). |
| 4. Decision absorbed       | A vault note for the resolved decision exists AND a future run's retriever surfaces it (see `tests/glue/test_decision_cross_run_reuse_e2e.py` + `tests/glue/test_b13_decision_reuse_in_run.py`). |

### The cross-cutting PROVED invariant

`tests/execution/test_proved_invariant.py` codifies the load-bearing
anti-regression: **a verified `Deliverable` exists IFF a PASSED
`VerificationResult` is linked to the same `run_id` AND the run's
`WorkStep.proof_state` is `PROVED`.** This is the seam where the original
hollow ship happened — `write_verified_deliverable` does NOT itself enforce
the link, so the test sits over the helper's call surface (both the native
loop and the executor path) and assert the link at runtime. It also
structurally pins the SET of known callers so any new caller goes through
explicit review.

### Anti-patterns (do not ship)

- Asserting `run.status is RunStatus.REVIEW_READY` without asserting a
  PASSED `VerificationResult` exists for the run.
- Asserting `Deliverable` row count without asserting `payload.artifact_refs`
  is non-empty (the legacy hollow path wrote `artifact_refs=[]`).
- Stubbing the retriever without asserting the canon TEXT appears in the
  consumer (messages, contract). "Retriever was called" is necessary but
  not sufficient; the content must FLOW through.
- Asserting `VerificationResult` row count without asserting `outcome`. A
  written-but-FAILED result is honest; a row count of 1 by itself doesn't
  distinguish honest fail from fake pass.

### FK-safe seeding

Real Postgres enforces foreign keys; SQLite tolerates orphans. Tests that
seed `Decision`, `WorkStep`, `RunAttempt`, etc. MUST seed their parent
`ExecutionRun` (and, for connector-bound runs, the parent `Request` /
`TriggerEvent`) first, or the suite will pass on SQLite and fail on PG.
The shared `tests/_support.py::db_engine` fixture handles cleanup ordering;
within a test, follow the same parent-before-child order on inserts.

### When the helper layer doesn't enforce an invariant

If a contract is enforced only by callers (e.g. "the caller must pass a
PASSED `VerificationResult.id` to `write_verified_deliverable`" — which the
helper does NOT validate), add BOTH:

1. a runtime invariant check (the cross-cutting end-of-test sweep, as
   `_assert_proved_invariant` does), AND
2. a structural caller-surface pin (the explicit known-callers set in
   `test_known_call_sites_are_in_expected_modules`) so a new caller forces
   a human re-review of the verify gate.

The runtime check is the proof; the structural check is the smoke detector
for when the proof's assumptions might no longer hold.
