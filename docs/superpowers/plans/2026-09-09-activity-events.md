# PR E (backend): Versioned Live Activity Event Contract

## 1. Shipped

`orchestrator/activity.py`: `schema_version: 1` envelope (type, event_id,
run_id, session_id, sequence, timestamp, severity + payload), monotonic
per-run sequencing via `ActivityEmitter`, legacy translation for
`step`/`plan`/`stats` (fields verbatim + envelope; envelope wins
collisions), full 26-type vocabulary declared up front.

Wired: step lines (agent announce), plan + stats lines (cli), plus new
`run_completed` / `run_failed` (agent end, abnormal endings included) and
`memory_written` (preference-write seam). Verified live against the sim
driver: legacy keys intact (panel parses unchanged), sequences 0..N
monotonic, run_completed closes with outcome/steps.

UI impact: none required. The panel ignores unknown types and extra
fields by contract (verified in menu.html `renderStepEvent`), so the
timeline renderer (§9) can land independently.

## 2. Deliberately deferred

- **Timeline renderer (§9)** + menu.html modularization (§10): needs
  visual verification on a live panel; backend-first keeps it unblocked.
- **Unwired vocabulary**: `skill_mounted/updated` (needs loop hooks),
  `approval_state` (needs approvals hookup), `goal_proposed/selected`
  (needs scheduler hooks), `decision_ready` et al. Names exist so
  producers and panel share one list; wire producers before relying on
  any of them.
- **`session_id` population**: empty until a session concept is passed
  down (autonomous nights); a single run reports no session rather than
  inventing one.
- **step `screenshot` key**: still null on the stream (frames stay out of
  stdout by design); the trace file keeps filenames.
