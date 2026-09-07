# Benchmark Integrity Hardening Plan

**Goal:** Make the frozen v1 benchmark fail closed when any scenario semantics or manifest structure drift, and prevent the harness from accepting records for the wrong scenario/attempt.

**Scope:** `src/computeruse/benchmark/manifest.py`, `src/computeruse/benchmark/harness.py`, `tests/smoke/test_benchmark.py`. No scenario prompt or success criterion is intentionally changed.

## Task 1: Pin all benchmark semantics

1. Add RED tests proving that changing `start_state`, `expected_outcome`, `success_criteria`, and other Scenario fields changes `manifest_hash`.
2. Replace the ID+prompt-only canonical payload with the complete `Scenario` payload.
3. Update the frozen hash after verifying the scenario data itself is unchanged.

## Task 2: Fail closed on malformed/version-mismatched manifests

1. Add RED tests for a top-level `manifest_version` that differs from `MANIFEST_VERSION`.
2. Add RED tests showing non-object scenario entries are rejected instead of silently skipped.
3. Require non-empty identity/execution metadata (`name`, `category`, `difficulty`, `start_state`) while keeping `notes` optional.
4. Implement the smallest loader validation needed for those tests.

## Task 3: Prevent harness record cross-contamination

1. Add RED tests where `execute()` returns a record with the wrong `scenario_id` or `attempt`.
2. Make `run_suite` reject those records immediately instead of corrupting aggregate metrics.

## Task 4: Verification and integration

1. Run targeted benchmark tests.
2. Run ruff, pyright, full pytest, Rust tests/clippy, and macOS CI gates through the repository's official CI.
3. Review the final diff for scope creep.
4. Merge only after the PR is green, then verify the post-merge `main` CI.

## Follow-up, deliberately separate

After this PR, audit benchmark scoring semantics. In particular, distinguish capability success from expected terminal outcomes such as `interrupted` (kill-switch) and `confirmation_required` (destructive-action guard), rather than mixing those concerns into the freeze-integrity change.
