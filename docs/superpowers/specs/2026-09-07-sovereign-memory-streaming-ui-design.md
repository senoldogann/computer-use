# Sovereign Autonomy, Adaptive Memory, Performance, and Live Activity UI

**Status:** Proposed design, approved in chat and awaiting repository-spec review before implementation planning.  
**Date:** 2026-09-07  
**Scope:** `computer-use` runtime, learning/memory, structured event stream, and menu-bar WebView UI.

## 1. Context

The project already has the core pieces needed for a much more autonomous product:

- `src/computeruse/autonomous.py` can choose work from memory and run unattended while the machine is idle.
- Episodic and semantic stores retain prior outcomes and reusable application knowledge.
- Successful trajectories can be distilled into reusable skills.
- `@@CU` structured events already cross the Python → Rust → WebView boundary.
- `driver/assets/menu.html` already renders plans, steps, stats, tools, observations, and run activity.

The problem is therefore not “invent an autonomous agent from zero.” The problem is to turn those pieces into one coherent operating mode, make learning deliberate and auditable, create a fast path for known workflows, and replace the current 305 KB single-file UI source with maintainable modules without changing the visual identity.

The user also wants the live run to feel transparent in the same practical sense as a modern agent activity timeline: while a task is running, the UI should continuously show what stage the system is in, what it observed, what it chose to do, what tool ran, what came back, what was verified, what failed, what was retried, what was learned, and why the run stopped.

This design explicitly does **not** expose raw hidden chain-of-thought. The UI exposes concise, structured operational rationale that is already appropriate for logs and audit: goal, sub-goal, observation summary, selected action, tool/result, verification, recovery, memory/skill updates, timing, and final outcome.

## 2. Goals

1. Add an explicit **Sovereign Mode** in which the agent may choose and execute work without per-action confirmation, including destructive actions, after the user has intentionally enabled that mode for the session.
2. Keep non-negotiable runtime controls independent of model decisions: kill switch, hard budgets, wall-clock ceilings, audit trail, and secret-memory filtering.
3. Improve self-directed work selection so the agent can learn what is useful to the user rather than only retrying failed or unproven skills.
4. Make memory adaptive, confidence-scored, provenance-aware, reversible, and resistant to one-off noise.
5. Make repeated tasks faster by using verified skills and cached knowledge before falling back to a full expensive OODA/model path.
6. Replace ad-hoc/prose UI streaming with a versioned structured activity-event contract.
7. Refactor `menu.html` into maintainable source modules while preserving the shipped visual design and preserving a single embedded HTML artifact for Rust.
8. Make long-running live activity readable, grouped, expandable, selectable, and performant.
9. Deliver changes in small, independently testable PRs rather than a single architectural rewrite.

## 3. Non-goals

- Replacing the Rust driver process or Unix-socket actuation architecture.
- Replacing Accessibility-first grounding or visual verification.
- Replacing the existing skill/episodic/semantic stores wholesale with a vector database in this phase.
- Turning every internal log line into UI content.
- Displaying raw model chain-of-thought.
- Rewriting the menu UI in React, SwiftUI, or another framework merely for fashion. The current WebView is adequate if its source is modularized.
- Allowing the model, a webpage, an MCP tool, or prompt content to enable Sovereign Mode by itself.

## 4. Architecture Overview

The target architecture adds four explicit layers around the existing runtime:

```text
Operator / UI
    |
    v
AutonomySessionPolicy
    |-- guarded / full / sovereign
    |-- immutable safety floor
    v
AutonomousScheduler ----> UserModel / Memory ----> Skill Registry
    |                          |                       |
    |                          +---- learning --------+
    v
Goal / Plan / OODA Runner ---- FastPathExecutor
    |                               |
    +----------- EventBus <---------+
                    |
          versioned ActivityEvent
                    |
             stdout @@CU
                    |
              Rust menu bridge
                    |
          modular WebView timeline
```

The existing execution engine remains authoritative for observation, action, verification, retries, and completion. New layers compose around it rather than bypassing it.

## 5. Sovereign Mode

### 5.1 Separate capability, not a renamed `--yes`

Sovereign Mode must be a distinct autonomy mode, not another interpretation of trust mode.

Proposed policy enum:

- `OBSERVER`
- `SUPERVISED`
- `GUARDED`
- `FULL`
- `SOVEREIGN`

`FULL` retains the current destructive-confirmation boundary. `SOVEREIGN` is the explicit “the operator has delegated this session” mode.

### 5.2 Activation rules

Sovereign Mode may only be enabled through a trusted local configuration surface:

- explicit CLI flag, e.g. `--sovereign`, or
- an explicit local menu/UI control.

It must not be switchable from:

- model output,
- webpage text,
- MCP/tool output,
- memory,
- skill content,
- remote prompt content.

The mode is latched at session start and included in every run/session event.

### 5.3 Safety floor that Sovereign Mode cannot disable

Even Sovereign Mode keeps these controls mandatory:

- global kill switch,
- physical user-reclaim signal,
- max wall-clock duration,
- token/cost/run ceilings,
- driver liveness boundaries,
- completion verification,
- trace/audit emission,
- secret filtering before memory writes,
- path/transport/tool sandbox boundaries that are policy-independent.

These are not “permissions.” They are runtime invariants.

### 5.4 Destructive actions

Under Sovereign Mode, `Risk.DESTRUCTIVE` may resolve to `ALLOW` without per-action approval. The runtime must still:

- classify the actual machine target rather than trusting model prose,
- emit a `safety_decision` activity event,
- include risk, target identity, and policy source in the trace,
- remain interruptible before dispatch,
- verify the resulting machine state when a postcondition exists.

This preserves full autonomy without making destructive actions invisible.

## 6. Self-Directed Work and User Learning

### 6.1 Replace random memory retry with ranked work proposals

The current autonomous proposer is intentionally conservative: failed episodes, demoted skills, then unproven skills. Keep those sources, but add a scored candidate layer.

Candidate sources:

1. explicit pending user goals,
2. interrupted/failed missions,
3. demoted or low-confidence skills worth repairing,
4. repeated successful tasks worth consolidating,
5. preference-backed recurring work,
6. maintenance work generated from concrete evidence, such as stale skill validation.

No candidate may be invented from an unconstrained “do something useful” prompt. Every autonomous goal must carry provenance.

Proposed `GoalProposal` additions:

- `source_type`
- `source_id`
- `utility_score`
- `confidence`
- `expected_cost`
- `reason`

Ranking should be deterministic given the same state, with randomized tie-breaking only where scores are equal.

### 6.2 User model

Add a small, typed user-preference model rather than stuffing all observations into generic semantic memory.

Suggested preference fields:

- `preference_id`
- `domain` (UI, workflow, communication, app, scheduling, formatting, etc.)
- `key`
- `value`
- `confidence`
- `evidence_count`
- `source` (`explicit`, `repeated_behavior`, `successful_correction`)
- `first_seen`
- `last_seen`
- `supersedes`

Rules:

- Explicit user instructions have the highest confidence.
- One observed behavior does not become a durable preference by itself.
- Inferred preferences require repeated consistent evidence.
- Contradictory evidence reduces confidence or supersedes the old record; it does not silently overwrite history.
- Passwords, API keys, tokens, authentication material, raw private message bodies, clipboard secrets, and other credential-like content are never stored as preferences.
- Every automatic memory write emits a `memory_written` event with a safe summary and provenance.

### 6.3 Learning loop

At the end of a verified run:

1. write episode,
2. update skill success/failure confidence,
3. extract safe semantic facts,
4. evaluate whether a user preference gained meaningful evidence,
5. optionally distill/update a reusable skill,
6. emit learning events.

Learning only occurs from verified outcomes. A false-success, forced finish, or unverifiable result cannot increase skill or preference confidence.

## 7. Performance: Fast Path Before Full OODA

The largest useful speedup is to avoid an LLM round-trip when the system already knows a high-confidence, verified workflow.

### 7.1 Execution tiers

For each sub-goal:

1. **Deterministic direct path**: exact local operation/tool with explicit contract.
2. **Verified skill path**: high-confidence skill whose environment fingerprint still matches.
3. **Normal OODA path**: observe → model decision → act → verify.
4. **Recovery path**: invalidate stale fast-path assumptions and fall back to OODA.

### 7.2 Skill confidence

A skill should track:

- successful uses,
- failed uses,
- consecutive successes,
- last successful timestamp,
- last environment/app/site identity,
- last failure reason.

Fast path is only eligible above a configurable confidence threshold and when the current environment still matches the skill’s semantic targets.

No raw coordinate replay is introduced. Stable AX identities and semantic targets remain primary.

### 7.3 Context and observation efficiency

Performance work should also include:

- cache semantic-memory retrieval per goal/app until relevant state changes,
- avoid duplicate AX/OCR work within one stable observation generation,
- batch UI event delivery,
- keep tool-result previews bounded,
- reuse existing provider/session transport where supported,
- measure per-phase latency and optimize based on trace data, not intuition.

## 8. Versioned Live Activity Event Contract

The current `@@CU` transport is the right foundation but needs an explicit versioned schema.

### 8.1 Envelope

Every UI-facing event should use a common envelope:

```json
{
  "schema_version": 1,
  "type": "tool_completed",
  "event_id": "evt_...",
  "run_id": "...",
  "session_id": "...",
  "sequence": 17,
  "timestamp": "...",
  "severity": "info",
  "payload": {}
}
```

`sequence` is monotonic within a run so the WebView can render deterministically even when stdout/stderr delivery timing differs.

### 8.2 Event vocabulary

Initial stable event set:

- `session_started`
- `session_stopped`
- `goal_proposed`
- `goal_selected`
- `plan_created`
- `subgoal_started`
- `observation_ready`
- `decision_ready`
- `action_started`
- `action_completed`
- `tool_started`
- `tool_completed`
- `verification_started`
- `verification_passed`
- `verification_failed`
- `recovery_started`
- `recovery_completed`
- `safety_decision`
- `approval_state`
- `memory_retrieved`
- `memory_written`
- `skill_mounted`
- `skill_updated`
- `usage_updated`
- `run_completed`
- `run_failed`

Existing `step`, `plan`, and `stats` records are supported during migration and translated to the new model.

### 8.3 Operational rationale, not hidden reasoning

`decision_ready` exposes:

- concise observation summary,
- sub-goal,
- selected action,
- short operational rationale suitable for an audit log.

It does not expose raw hidden model reasoning or private chain-of-thought.

## 9. Live Timeline UX

The activity view should read as one continuous task narrative rather than a terminal dump.

### 9.1 Visual hierarchy

Top level:

- current goal,
- status,
- autonomy mode,
- elapsed time,
- token/cost counters,
- stop/kill affordance.

Timeline groups:

- Planning
- Research / Tools
- Computer actions
- Verification
- Recovery
- Learning
- Completion

Each group is collapsed by default when completed, while the currently active group remains expanded.

### 9.2 Row behavior

A row can display:

- small icon/type,
- human-readable title,
- state (`running`, `done`, `failed`, `waiting`),
- elapsed duration,
- expandable details.

Examples:

- “Searched 4 sources”
- “Opened Notes”
- “Verified note title and 5 items”
- “Recovery: stale screen, re-observed target”
- “Learned preference: concise summaries”

Tool arguments/results and low-level action payloads live in the expanded detail surface, not in the primary feed.

### 9.3 Streaming behavior

- Events render incrementally as they arrive.
- One active “working” row may update in place rather than append a new spinner every second.
- High-frequency progress events are coalesced.
- Auto-scroll follows only while the user is already at the bottom; manually scrolling upward never gets stolen back.
- Long output is virtualized or bounded in the DOM.
- Feed text remains selectable/copyable.
- Errors remain visible and are never replaced by a later success row.

## 10. `menu.html` Modularization

The current ~305 KB source file contains structure, styles, state, native bridge code, renderers, event parsing, and UI behavior in one unit. That makes changes risky and review difficult.

Do **not** change the Rust embedding contract unnecessarily. Instead, change the source layout and generate one deterministic artifact.

Proposed source tree:

```text
driver/ui/
  menu.template.html
  styles/
    tokens.css
    shell.css
    timeline.css
    controls.css
    cards.css
  scripts/
    state.js
    bridge.js
    events.js
    renderers/
      timeline.js
      plan.js
      tool.js
      verification.js
      memory.js
    interactions.js
    app.js

driver/assets/
  menu.html        # generated artifact used by include_str!
```

Build step:

```text
ui source modules
   -> deterministic bundle script
   -> driver/assets/menu.html
   -> Rust include_str!
```

Requirements:

- no runtime network dependency,
- no CDN,
- no npm bundler required unless a real need appears,
- deterministic output,
- generated artifact checked for drift in CI,
- same CSP/security behavior as today,
- visual regression comparison before/after.

A small Python or Rust bundler is enough: inline CSS and JS in a fixed order into the template. The goal is maintainability, not introducing a frontend toolchain empire.

## 11. Data Flow

### 11.1 Normal run

```text
Goal
 -> memory/skill retrieval
 -> plan/sub-goal
 -> observation
 -> fast-path eligibility check
 -> action/tool execution
 -> verification
 -> ActivityEvent emission throughout
 -> completion audit
 -> episode/skill/preference learning
 -> run_completed
```

### 11.2 Autonomous session

```text
Session starts in SOVEREIGN
 -> wait until eligible to act
 -> collect grounded candidate goals
 -> rank by utility/confidence/cost
 -> select one
 -> execute normal run
 -> learn
 -> rest/yield
 -> repeat until session stop/budget/kill switch
```

## 12. Error Handling and Recovery

- Event emission is best-effort for UI rendering but trace persistence remains independent.
- UI failure cannot stop the agent runtime.
- Malformed UI events are displayed as diagnostic fallback rows and do not crash the feed.
- Fast-path failure invalidates that attempt and falls back to normal OODA rather than repeatedly replaying it.
- A skill that repeatedly fails is demoted and removed from fast-path eligibility.
- Memory corruption remains isolated per entry.
- Sovereign session errors terminate the current goal according to existing recovery policy, not the entire session unless the error is a hard runtime invariant (kill switch, budget, driver unrecoverable, etc.).

## 13. Security and Privacy Invariants

Sovereign autonomy increases capability, so the following remain explicit invariants:

1. Sovereign mode is operator-enabled only.
2. Kill switch cannot be disabled by the model.
3. Budgets cannot be raised by the model.
4. Tool/MCP trust boundaries remain enforced independently.
5. Credentials and secret-like values are filtered from memory and live UI previews.
6. Raw typed content is not automatically persisted as learned preference data.
7. Learning records retain provenance and can be inspected/deleted.
8. Trace and UI event streams never claim success before completion verification.
9. Prompt/web/tool content cannot change the active autonomy policy.

## 14. Testing Strategy

### 14.1 Sovereign mode

- policy table tests for every autonomy/risk combination,
- explicit proof that `FULL` still confirms destructive work,
- explicit proof that `SOVEREIGN` allows it without per-action confirmation,
- kill-switch and budget tests under Sovereign Mode,
- proof that prompt/tool/model content cannot toggle the mode.

### 14.2 Learning

- explicit preference outranks inferred preference,
- single observation does not create durable inferred preference,
- repeated evidence increases confidence,
- contradiction decreases/supersedes confidence,
- secret-like content never persists,
- unverified run never promotes memory/skill confidence.

### 14.3 Fast path

- high-confidence matching skill avoids model call,
- stale semantic target forces OODA fallback,
- failed fast path demotes confidence,
- no coordinate-only replay.

### 14.4 Event stream

- schema/version validation,
- sequence monotonicity,
- compatibility with legacy `step/plan/stats`,
- bounded tool previews,
- redaction tests,
- completion/error events cannot reorder into false success.

### 14.5 UI

- generated `menu.html` is deterministic,
- CI fails on bundle drift,
- existing controls still work,
- event fixtures render expected rows,
- collapse/expand and autoscroll behavior,
- large synthetic run does not create unbounded DOM growth,
- screenshot/visual regression before and after modularization.

## 15. Delivery Plan / PR Boundaries

Implementation should be split into these PRs, each GREEN and merged before the next depends on it:

1. **PR A — Sovereign policy core**
   - add mode and policy tests,
   - CLI/UI activation plumbing,
   - immutable safety-floor tests.

2. **PR B — Versioned activity events**
   - typed event envelope and emitter,
   - compatibility adapter for existing `@@CU` records,
   - Rust bridge stays transport-only.

3. **PR C — Adaptive user memory**
   - typed preference store,
   - confidence/provenance rules,
   - secret filtering,
   - learning lifecycle events.

4. **PR D — Autonomous scheduler upgrade**
   - grounded candidate pool,
   - utility/confidence/cost ranking,
   - session-level learning loop.

5. **PR E — Verified fast path**
   - skill confidence metadata,
   - direct/skill/OODA tier selection,
   - fallback/demotion behavior,
   - latency benchmark.

6. **PR F — UI source modularization**
   - split CSS/JS/template,
   - deterministic bundler,
   - generated artifact drift check,
   - zero intentional visual change.

7. **PR G — Live activity timeline UX**
   - grouped event renderers,
   - incremental status rows,
   - verification/recovery/memory visibility,
   - DOM performance controls.

8. **PR H — End-to-end sovereign benchmark**
   - long autonomous session fixture,
   - memory reuse across runs,
   - safety-floor interruption,
   - performance and transparency baseline report.

## 16. Acceptance Criteria

The project is considered complete for this program when all of the following are demonstrated:

- A user can explicitly start a Sovereign session and no destructive step requires per-action approval.
- The global kill switch interrupts that session and prevents subsequent physical actions.
- Autonomous goals are all traceable to concrete stored evidence/provenance.
- The system learns stable user preferences from repeated evidence and can show why each preference exists.
- Secrets do not enter learned memory.
- A previously verified repeat workflow can execute through the fast path with fewer model turns than the cold run.
- A stale fast-path assumption falls back safely to OODA.
- Every major lifecycle phase is visible as a typed activity event.
- The UI shows a clean live timeline with planning, tools/actions, verification, recovery, learning, and completion.
- The UI does not expose hidden chain-of-thought.
- `menu.html` is generated from maintainable modules and has no intentional visual regression.
- Full Python/Rust CI, macOS real-backend gates, new regression tests, and the frozen benchmark suite are GREEN.

## 17. Migration and Compatibility

- Existing `FULL` behavior remains unchanged.
- Existing memory files remain readable.
- New preference/skill metadata uses defaults for old records.
- Existing `@@CU` `step`, `plan`, and `stats` records remain supported while the UI moves to the versioned event envelope.
- `driver/assets/menu.html` remains the Rust-embedded output path, so native driver loading does not need an architectural rewrite.
- Each migration is additive first; destructive cleanup of old event/rendering code happens only after fixtures prove parity.

## 18. Design Rationale

This design chooses evolution over replacement. The repository already has the correct major primitives: an OODA runtime, verified execution, autonomous sessions, durable memory, skill distillation, structured events, a Rust transport bridge, and a WebView. The professional path is to make their contracts explicit and composable.

The key architectural decisions are therefore:

- **Sovereign is a session policy, not a prompt instruction.**
- **Learning is confidence + provenance, not “remember everything.”**
- **Speed comes from verified reuse, not skipping verification.**
- **Transparency comes from typed lifecycle events, not parsing prose logs.**
- **The UI source becomes modular, while the shipped Rust asset stays simple.**
- **Raw chain-of-thought is not part of the product surface; operational evidence is.**

These decisions keep the system powerful without making it opaque, fragile, or impossible to audit.