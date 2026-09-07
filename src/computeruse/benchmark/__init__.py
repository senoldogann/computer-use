"""Frozen live-host benchmark: versioned scenarios, repeatability harness.

A benchmark number is only meaningful if the test it came from cannot drift.
This package owns the three things that make a rerun comparable:

* :mod:`computeruse.benchmark.manifest` — the frozen scenario list (exact
  prompts, start state, expected outcome, checkable success criteria) plus
  the outcome vocabulary. Any prompt change alters the manifest hash, and
  the freeze test fails until the version is bumped deliberately.
* :mod:`computeruse.benchmark.harness` — the repeatability runner: clean
  start state per attempt, per-run records, and aggregate metrics (pass@1,
  expected-outcome rate, medians, recovery/auditor/false-success/safety
  counts). Runners are injected, so the math is unit-testable offline;
  live execution is operator-driven and never part of CI.
"""
