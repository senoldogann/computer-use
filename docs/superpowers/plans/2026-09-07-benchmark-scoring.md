# Benchmark Scoring Semantics Plan

**Goal:** Make benchmark metrics distinguish task-completion success from matching a scenario's expected terminal outcome.

**Problem:** `expected_outcome_rate` currently counts only actual outcomes named `expected_pass` or `expected_fail`. It cannot compare actual vs expected because `RunRecord` carries no expected outcome. That incorrectly penalizes safety scenarios whose correct terminal states are `interrupted` or `confirmation_required`.

## Task 1: Pin the scoring semantics with RED tests

1. A kill-switch record with expected=`interrupted`, actual=`interrupted` must count toward `expected_outcome_rate` but not `pass_at_1`.
2. A destructive-guard record with expected=`confirmation_required`, actual matching it must behave the same way.
3. An expected/actual mismatch must reduce `expected_outcome_rate`.
4. `false_success=True` must prevent a record from counting as an expected-outcome match even when the strings match.
5. Aggregation must fail closed when expected-outcome provenance is missing.
6. `run_suite` must attach the manifest's expected outcome to each accepted record and reject a conflicting executor-supplied expectation.

## Task 2: Implement the smallest self-contained record contract

1. Add optional `expected_outcome` to `RunRecord` so raw executors can return records without duplicating manifest data.
2. In `run_suite`, validate any supplied expectation and then attach the scenario's frozen expectation using `dataclasses.replace`.
3. Validate actual and expected outcomes against `OUTCOME_VOCABULARY` before aggregation.
4. Keep `pass_at_1` as the existing task/capability metric. Do not relabel safety interruptions or approval parking as task success.
5. Compute `expected_outcome_rate` strictly from actual==expected, poisoned by `false_success`.

## Task 3: Verify and integrate

1. Run targeted benchmark tests through official CI.
2. Run full ruff, pyright, pytest, Linux Rust and macOS backend gates.
3. Review final diff for scope creep; no manifest/scenario changes belong in this PR.
4. Squash merge only when green, then verify main CI.
