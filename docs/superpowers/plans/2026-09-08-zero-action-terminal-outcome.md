# Zero-action terminal outcome follow-up

## Problem

`OodaRunner._finalize` currently suppresses `on_complete` when a run reaches a terminal `finish` without any executed action and without a mounted skill. `Agent` therefore never receives the verified terminal outcome and reports `succeeded=False`, even when the provider correctly reports that the requested state already exists.

This is not an adaptive-preference-memory defect: preference memory already writes only inside `Agent.on_complete` after a verified non-empty trajectory. The bug is the older terminal-callback contract conflating two independent questions: whether the run has a terminal outcome, and whether there is a trajectory worth persisting.

## Required behavior

- A verified `finish(success)` with zero executed actions reports `AgentResult.succeeded=True`.
- The terminal callback fires exactly once even for an empty trajectory.
- An empty trajectory creates no episode, semantic fact, distilled skill, or preference.
- A mounted skill that enables an immediate finish can still receive its success reinforcement.
- `finish(failed)` remains failure.
- Forced/unverified finishes remain failure and cannot learn.

## Implementation constraint

Make this as a dedicated small orchestration-contract change, not as part of PR #68. The preferred fix is to separate terminal outcome publication from persistence eligibility rather than special-casing preference memory.

## Verification

Add focused OODA and Agent regressions first, then run Ruff, Pyright, the complete pytest suite, Rust tests/clippy, and macOS real-backend CI before merge.
