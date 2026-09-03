# AGENTS.md — Autonomous Physical Computer-Use Framework Constitution

## 1. Vision & Core Mission

This repository is dedicated to building an **Autonomous, Human-Centric, Self-Evolving Computer-Use System**. 

Unlike conventional browser-automation tools, headless wrappers, or cloud-sandboxed agents (e.g., standard Claude Computer Use, Codex, browser-use), this system operates directly on the **physical host environment**. It interacts with the operating system exactly as a human would: by perceiving pixels, understanding visual layout and coordinates, moving the physical cursor along human-like trajectories, and interacting with native running desktop applications (e.g., clicking real Dock icons, interacting with actual user browser profiles, native desktop applications, and system dialogues).

The framework is designed under the **Prompt & Orchestration Supremacy** principle: the scaffolding, reasoning loops, memory mechanisms, and skill synthesis engines must be so resilient that **even a weaker or smaller LLM becomes highly capable, reliable, self-correcting, and continuously improving**.

---

## 2. The 6 Immutable Laws (Project Constitution)

Every line of code, agent decision, and architectural module within this repository **MUST** conform to these 6 laws without exception.

### Law 1: Physical Reality & Natural Actuation (No Synthetic Bypasses)
1. **Direct Host Interaction**: The agent must never use synthetic backdoor shortcuts (e.g., launching isolated headless browser instances with clean profiles) when instructed to perform a task. It must interact with the user's real desktop environment.
2. **Visual Spatial Grounding**: Actions must be spatially grounded through the host accessibility tree (AX, the primary source — see ADR-2) or visual perception (OCR/coordinate mapping, the fallback for apps without an AX tree), and confirmed by a visual diff before execution when verification is enabled. Axes are DPI/Retina-aware.
3. **Human-like Kinematics**: Mouse movements must follow natural, continuous trajectories (cubic Bezier curves with distance-adaptive duration, realistic velocity profiles, variable typing speeds, post-click micro-pauses) rather than instantaneous coordinate teleportation. While the agent runs, a translucent emerald halo follows the cursor and a menu-bar status item shows the agent is active (Law 5.2: the user always sees what the agent is doing).

### Law 2: Model-Agnostic Orchestration & Scaffolding Supremacy
1. **Weak-Model Resilience**: Never assume the underlying LLM has advanced native tool-use reasoning. The system must provide structured prompts, strict input/output contracts (JSON/Pydantic schemas), validation gates, and step-by-step reflection steps (Observe-Orient-Decide-Act / OODA).
2. **Active Self-Correction**: If a model generates invalid actions or hallucinates coordinates, the framework must catch the failure, compute visual diffs, inject localized error diagnostics, and guide the model back to the intended path. Repeated identical actions with no progress trip a stuck-loop guard: the 3rd repetition injects a corrective error, and the 5th aborts the run outright — a run can never click forever.

### Law 3: Dynamic Skill Distillation & Two-Stage Context Retrieval
1. **Automatic Skill Extraction**: When the system successfully executes a new complex multi-step workflow (e.g., navigating a specific application, exporting a report, handling a specific UI flow), it must distill the interaction into a structured **Skill definition**.
2. **Zero Context Bloat (Two-Stage Retrieval)**:
   - **Stage 1 (Summary Scan)**: The agent context contains *only* lightweight metadata and 1-2 line descriptions of available skills.
   - **Stage 2 (On-Demand Loading)**: Only when the agent identifies a skill as relevant to the current sub-goal does it fetch and mount the full skill instructions and coordinate schemas into active context via `load_skill(skill_id)`.
3. **Continuous Skill Evolution**: Existing skills must be updated with refined coordinates, alternative UI states, or shortcut paths whenever repeated executions discover optimizations or handle UI updates.

### Law 4: Multi-Tiered Memory & Experiential Continuity
1. **Episodic Memory**: Full trace of past sessions, successful task trajectories, and failure retrospectives saved in structured formats. Above the episode sits the **mission** (`orchestrator/mission.py`): the durable work item, carrying its plan and therefore its progress across the runs that attempt it. Resuming hands over `remaining_goal` — what is actually left — never the original goal, because on a physical host re-running a completed sub-goal is not wasted work, it is that step happening again.
2. **Semantic Knowledge Store**: Searchable memory index containing application-specific UI patterns, user preferences, coordinate maps, and shortcut behaviors.
3. **Working Context (Scratchpad)**: Minimal, clean, rolling state tracking current goal, completed steps, pending sub-tasks, and latest visual diffs.

### Law 5: Explicit Permission Governance & Security Boundaries
1. **Configurable Autonomy Levels**:
   - **Level 0 (Observer / Advisory)**: Recommends actions and highlights UI regions without taking control.
   - **Level 1 (Supervised / Step-by-Step)**: Proposes each action and waits for user confirmation before physical execution.
   - **Level 2 (Guarded Autonomy)**: Executes routine actions autonomously; pauses and prompts user confirmation for destructive actions (e.g., file deletion, payments, terminal commands, email dispatch).
   - **Level 3 (Full Autonomy / Auto Mode)**: Unattended execution with continuous self-monitoring, safety boundary checks, and automatic fallback on anomalies. A destructive action still requires a human at Level 3 — but unattended, "requires a human" cannot mean "wait at a prompt nobody will answer". The run **parks** it: the proposed action, the control it targets, the sub-goal it serves and the guard's classification are written to the approval queue (`security/approvals.py`), the mission is marked `blocked` rather than `failed` (`orchestrator/mission.py`, so the attempt is refunded and the work stays resumable), and the session moves on. The same question is never asked twice while it is unanswered. A human answers with `--approve`/`--deny`, which returns the mission to the queue; the approval is a *recorded answer*, never a remote control — the next run reaches that step itself.
   - **Delegating in advance (`security/grants.py`)**: answering the same question every night is not autonomy either. A **capability grant** is bounded authority written ahead of time — a verb family (delete / send / pay / install / overwrite / admin / shell, matched across languages so an English grant covers a Turkish button), an application, a glob over control titles, a use count that is really decremented, and an expiry. What a grant may do is deliberately tiny: turn one `CONFIRM` into one `ALLOW` for a `DESTRUCTIVE` action that matches it. It can never lift a `BLOCK`, never cover a merely routine action, and never apply at Level 0 or 1 — those levels exist in order to ask, so honouring a grant there would be a bypass rather than a delegation. Grants are minted with `--grant` (or `--approve <id> --always`), listed with `--grants`, and removed with `--revoke`.
2. **Accountability (`orchestrator/report.py`)**: unattended work is only defensible if the person who owns the machine can find out what happened on it. `--report --since-hours N` reads the five stores together — episodes, missions, approvals, grants and per-run usage — and prints what is waiting for a decision *first*, because that is the only part that will not resolve itself, then what is paused, what ran, what it spent and which delegated authority it used. Open items are never filtered by the window: a question parked three days ago is more urgent than one parked an hour ago. Spend is recorded per run on every ending, including failures — the run someone wants a number for is usually the one that went wrong.
3. **Emergency Kill-Switch**: The user must always be able to reclaim physical control instantly. Implemented channels: **Command+Shift+Escape** (global event-tap hotkey — the driver reports `hotkey_state` and aborts the current action), **SIGINT/SIGTERM** (Ctrl-C), and **rapid cursor shake** (fail-safe interrupt). When tripped, all queued actions are dropped and the run ends; the user can always just grab the mouse.

### Law 6: Architectural Purity & Code Standards
1. **Functional Core, Imperative Shell**: Pure data transformations and deterministic logic must be implemented via functional programming. Classes are reserved strictly for external OS connectors and I/O drivers.
2. **Strict Typing Everywhere**: All functions, return values, parameters, and collections must have explicit, concrete types (no `Any`, `unknown`, or untyped dictionaries).
3. **Explicit Error Propagation**: Never swallow exceptions silently. Log structured contextual diagnostics (state, coordinates, screenshot hash, active window) before re-raising or prompting recovery.

---

## 3. System Architecture & Component Diagram

```
                        ┌────────────────────────────────────────┐
                        │           User / Task Input            │
                        └──────────────────┬─────────────────────┘
                                           │
                                           ▼
                        ┌────────────────────────────────────────┐
                        │      Autonomy & Permission Guard       │
                        │    (Level 0 / 1 / 2 / 3 & Safety)      │
                        └──────────────────┬─────────────────────┘
                                           │
                                           ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│                           Agent Orchestration Engine                           │
│                                                                                │
│  ┌────────────────────────┐  ┌───────────────────────┐  ┌───────────────────┐  │
│  │ Vision & Spatial Input │  │  Multi-Tier Memory    │  │  Skill Registry   │  │
│  │ • Screenshot Capture   │  │  • Working State      │  │  • Summary Index  │  │
│  │ • Coordinate Scale/DPI │  │  • Episodic Trajectory│  │  • Lazy-Load      │  │
│  │ • Element Grounding    │  │  • Semantic Store     │  │  • Distiller      │  │
│  └───────────┬────────────┘  └───────────┬───────────┘  └─────────┬─────────┘  │
│              │                           │                        │            │
│              └───────────────────────────┼────────────────────────┘            │
│                                          ▼                                     │
│                        ┌──────────────────────────────────┐                    │
│                        │   Structured Reasoning (OODA)    │                    │
│                        │   • Weak-Model Scaffolding       │                    │
│                        │   • JSON / Action Plan Validator │                    │
│                        └─────────────────┬────────────────┘                    │
└──────────────────────────────────────────┼─────────────────────────────────────┘
                                           │
                                           ▼
                        ┌────────────────────────────────────────┐
                        │       Physical Actuation Engine        │
                        │   • Bezier Mouse Path Kinematics       │
                        │   • Natural Keystroke Cadence          │
                        │   • Focus & Window Management          │
                        └──────────────────┬─────────────────────┘
                                           │
                                           ▼
                        ┌────────────────────────────────────────┐
                        │    Host Operating System (macOS/OS)    │
                        │   Real Dock, Real Chrome, Real Apps    │
                        └────────────────────────────────────────┘
```

---

## 4. Standard Action Contracts (Pydantic / JSON Schemas)

To eliminate model hallucinations and ensure weak models always output valid commands, the orchestration engine enforces the following strictly-typed Action Contract:

```json
{
  "thought": "I need to open Google Chrome from the macOS Dock.",
  "sub_goal": "Click Google Chrome icon in Dock",
  "action": {
    "type": "mouse_click",
    "x": 284,
    "y": 880,
    "button": "left",
    "click_count": 1
  }
}
```

### Supported Action Types:
1. `mouse_move`: `{"type": "mouse_move", "x": int, "y": int, "duration_ms": int}` (Moves mouse via Bezier trajectory; `duration_ms` is a floor — the driver scales it with distance so long sweeps stay human-paced).
2. `mouse_click`: `{"type": "mouse_click", "x": int, "y": int, "button": "left"|"right"|"middle", "click_count": 1|2}`.
2b. `click_mark`: `{"type": "click_mark", "mark": int, "button": "left"|"right"|"middle", "click_count": 1|2}` (Preferred for anything the AX list names: `mark` is the `[N]` shown beside the element, and the orchestrator resolves it to that element's exact centre in logical points — no image-space rounding. `mouse_click` remains the fallback for targets AX does not list.)
3. `mouse_drag`: `{"type": "mouse_drag", "start_x": int, "start_y": int, "end_x": int, "end_y": int, "duration_ms": int}` (Same Bezier kinematics as `mouse_move`).
4. `mouse_scroll`: `{"type": "mouse_scroll", "dx": int, "dy": int}` (Scrolls **at the current cursor position** — emit a `mouse_move` first when the target matters).
5. `type_text`: `{"type": "type_text", "text": str, "wpm": int}` (Types text with human-like inter-key delays).
6. `press_hotkey`: `{"type": "press_hotkey", "modifiers": ["command"|"shift"|"alt"|"control"], "key": str}`.
7. `load_skill`: `{"type": "load_skill", "skill_id": str}` (Explicitly mounts a skill, replacing the auto-retrieved one — see RETRIEVE).
8. `wait`: `{"type": "wait", "duration_ms": int, "reason": str}`.
9. `finish`: `{"type": "finish", "status": "success"|"failed", "summary": str}`.
10. `clipboard_paste`: `{"type": "clipboard_paste", "text": str}` (Pastes text; semantically verified against the focused field's AXValue when determinable).
11. `activate_app`: `{"type": "activate_app", "app": str}` (Brings a running app to the front via LaunchServices; verified by the focused-window probe).

---

## 5. Execution State Machine (Autonomy Cycle)

Every agent step strictly executes the following cycle. The cycle is the OODA
loop with the two steps that decide whether a run is *reliable* made explicit:
VERIFY (did the action actually land?) and RECOVER (what happens when it did
not?).

```
[1. OBSERVE]   ──► Automatic, orchestrator-driven, and taken as ONE immutable snapshot
                   (Observation): the focused window, the frontmost app's AX element
                   summaries, and — when vision is enabled — a screenshot downscaled to
                   the exact map the model perceives. The snapshot carries its own
                   ScreenMap, so image-space and screen-space can never drift apart.
                   The model NEVER requests observation — it is injected before every
                   decision.
       │
[2. UNDERSTAND]──► The previous action's verdict and any recovery guidance are folded into
                   last_error; the progress signature of this snapshot is compared with the
                   one before the last action to answer "did anything actually move?".
       │
[3. RETRIEVE]  ──► Hybrid: the orchestrator scans the Skill Summary Index and auto-mounts the top
                   same-app match (zero extra model tokens). The model may override by emitting
                   load_skill for a different skill in DECIDE. Repeated failure UNMOUNTS a skill:
                   a workflow that keeps failing is actively misleading the model.
       │
[4. DECIDE]    ──► LLM generates structured Thought + Sub-goal + Action (or a small ordered batch).
       │
[5. VALIDATE]  ──► Four gates, all before any physical effect: the Permission Guard (Autonomy
                   Level + destructive-action check, classified from the accessibility title of
                   the control under the pointer — not from how the model described the click),
                   the coordinate gate (image space → screen
                   points via the snapshot's ScreenMap, display origin included), the fail-closed
                   bounds check against the observed display's own rectangle, and
                   the positional gate — one live window read that catches both focus drift (the
                   target app no longer owns the screen) and decision staleness (the host moved on
                   during the model's turn).
       │
[6. ACT]       ──► Physical Actuation Engine executes the action with human-like kinematics,
                   with the kill switch polled before, during and after.
       │
[7. VERIFY]    ──► Every action declares an expected postcondition, and independent witnesses
                   report on it: the AX surface (its element list AND a digest of its visible
                   text — one verdict, since both read one snapshot), the element under the click
                   holding focus, the focused field's AXValue, the frontmost app, and (with
                   --verify) a pixel diff. Rules: one CONFIRMED witness outweighs silent ones; a
                   witness that cannot speak is INCONCLUSIVE and NEVER fails an action; a
                   *direct* denial (the text is not in the field, the wrong app is frontmost) is
                   conclusive alone, while two *circumstantial* witnesses ("nothing changed") must
                   agree before an action is called a miss.
                   Change detection alone cannot judge an action that correctly changed nothing,
                   so two witnesses exist for that: focus landing on the clicked element vouches
                   for an idempotent click, and the text digest catches effects too small for a
                   pixel threshold and invisible to the element list. When all witnesses are
                   silent but an element covers the click point, the model is told the coordinate
                   was right and to check whether the goal is already met — not to re-aim.
       │
[8. RECOVER]   ──► A failure is classified (FailureKind), counted per signature, and answered with
                   escalating guidance: RETRY → ALTERNATE (change the method) → REPLAN (abandon the
                   tactic, unmount skills) → ABORT. The ladder is finite, so one obstacle can never
                   consume a whole run, and no single failure ends a run that could still recover.
       │
[9. FINISH]    ──► A claimed success is AUDITED before it is accepted: a separate, narrowly-scoped
                   read of the current screen (goal + claim + screenshot, without the actor's own
                   reasoning) must agree the goal is observably satisfied. A rejected claim folds
                   back as an ordinary recoverable error.
       │
[10. (post-run) DISTILL] ──► After the run completes, analyze the trajectory and synthesize a new
                             reusable Skill if novel. A run ended by max_steps, an unrecoverable
                             failure, or a kill-switch takeover NEVER distills — but it IS recorded
                             as a failed episode carrying a retrospective naming why it stopped, so
                             the work it did before the wall is not thrown away (Law 4.1).
```

**Coordinate-space invariant.** There is exactly one conversion between the
model's image space and the driver's logical screen points, and `ScreenMap`
owns both directions: `to_image` for everything perception hands the model (AX
rects included), `to_screen` for every coordinate the model hands back. The map
carries the captured display's global origin too, so the conversion is complete
on a secondary display (`--display N`): the screenshot's (0,0) is that
display's corner, actuation is global, and no caller can apply the scale and
forget the shift. The fail-closed bounds gate is evaluated against that
display's own rectangle, and AX elements on other displays are dropped before
the model ever sees them. A
screenshot whose ScreenMap cannot be computed is never shown to the model — a
frame with an unknown coordinate space produces confidently wrong clicks, which
is strictly worse than telling the model it is blind.

---

## 6. Project Architecture & Directory Layout

```
computeruse/
├── AGENTS.md                  # This constitution & system specification
├── pyproject.toml             # Python 3.12+ project configuration (uv)
├── driver/                    # Rust actuation micro-driver (ADR-1: separate process, never imported)
│   ├── Cargo.toml
│   └── src/
│       ├── main.rs            # Binary: socket accept loop; owns the AppKit main thread under --real
│       ├── protocol.rs        # JSON-RPC request/response enums (schema-validated contract)
│       ├── backend.rs         # Backend trait + SimulatedBackend (CI-safe, never touches the host)
│       ├── quartz.rs          # Real macOS backend: CGEvent mouse/keyboard, CGWindowList, open -a
│       ├── ax.rs              # Accessibility tree (AXUIElement) snapshot + focused-window probe
│       ├── bezier.rs          # Pure trajectory math: cubic Bezier, distance-adaptive duration
│       ├── hotkey.rs          # Kill-switch event tap (Command+Shift+Escape)
│       ├── indicator.rs       # Menu-bar status item + emerald cursor halo (AppKit, macOS only)
│       ├── menu.rs            # Menu-bar chat launcher: Liquid-Glass panel (WKWebView) + agent subprocess
│       ├── bin/menu.rs        # `actuation-menu` binary entry
│       └── lib.rs
├── driver/assets/
│   └── menu.html              # Liquid-Glass chat UI (bundled into the launcher)
├── src/computeruse/
│   ├── __main__.py            # Entry point: `uv run python -m computeruse ...`
│   ├── cli.py                 # CLI wiring: flags, driver spawn, error surfacing, summary output
│   ├── agent.py               # Agent: activate-on-start, OODA orchestration, distill-on-complete
│   ├── orchestrator/
│   │   ├── loop.py            # Autonomy cycle: observe/understand/retrieve/decide/validate/act/verify/recover
│   │   ├── evidence.py        # Expected postconditions + multi-witness verification verdicts (pure)
│   │   ├── failures.py        # Failure taxonomy + the bounded recovery ladder (pure)
│   │   ├── schemas.py         # Pydantic action contracts (discriminated unions, strict typing)
│   │   ├── prompts.py         # Scaffolding prompts & error-correction injectors
│   │   ├── untrusted.py       # Screen text as data: <observed_data> framing + escaping
│   │   ├── trace.py           # Per-step run trace (JSONL + optional step PNGs)
│   │   ├── budget.py          # Wall-clock / token / cost ceilings for one run
│   │   ├── planner.py         # Hierarchical goal decomposition + session checkpoints
│   │   └── client.py          # Typed JSON-RPC client for the Rust driver (Unix socket)
│   ├── providers/
│   │   └── openai.py          # LLM transport (model-agnostic seam: OpenAI-compatible)
│   ├── vision/
│   │   ├── ax.py              # ADR-2: AX element summaries, focus state, count/depth caps
│   │   ├── coordinates.py     # Retina-to-virtual coordinate transformation
│   │   ├── capture.py         # Display capture (verification only)
│   │   ├── diff.py            # Pre/post action visual diffing
│   │   ├── som.py             # Set-of-Marks: AX boxes drawn on the OBSERVE frame + numbered marks
│   │   └── focus.py           # Focused-app discovery + activation
│   ├── skills/
│   │   ├── registry.py        # Summary-index & lazy-loading engine (two-stage retrieval)
│   │   ├── distiller.py       # Trajectory-to-skill distillation (pure, de-duplicating)
│   │   └── schemas.py         # SkillDefinition / SkillSummary types
│   ├── memory/
│   │   ├── episodic.py        # Past execution traces & retrospectives
│   │   ├── semantic.py        # Key-value UI patterns & preferences
│   │   └── schemas.py         # Episodic/semantic memory payload types
│   └── security/
│       ├── autonomy.py        # Autonomy levels, permission guard, confirmation requirement
│       └── killswitch.py      # Interrupt detection & handoff
└── tests/
    ├── smoke/                 # End-to-end tests: driver RPC wire, OODA loop, grounding, CLI
    └── unit/                  # Pure data transformation tests
```

---

## 7. Development & Coding Rules for AI Assistants

When contributing code or modifying this repository, any AI assistant **MUST** follow these specific development constraints:

### 7.1 Language & Paradigms
- **Functional Paradigm**: Write pure, composable functions. Do not mutate input arguments or global state.
- **Classes**: Use OOP classes *only* for external system connectors (e.g., OS input drivers, screenshot capturers, LLM provider clients).
- **Strict Typing**:
  - Zero tolerance for untyped functions, implicit `Any`, or loose untyped dictionaries.
  - Define explicit Pydantic models or typed dataclasses for all payloads, action messages, and configurations.
- **No Multi-Mode Functions**: Functions must do one thing reliably. Avoid boolean flag parameters that branch function behavior into two different paths.

### 7.2 Error Handling & Resilience
- Always raise explicit, domain-specific errors (e.g., `CoordinateOutOfBoundsError`, `VerificationFailedError`, `StaleObservationError`, `FocusLostError`, `PermissionDeniedError`).
- Never use bare `except:` or catch-all exception blocks that suppress root causes.
- External API calls and OS input streams must incorporate exponential backoff with warning logs before raising the terminal error.
- Error payloads must contain rich debug context: target coordinates, screen dimensions, active window title, and action payload.

### 7.3 Documentation & Clean Code
- All comments, docstrings, and type definitions must be in **English**.
- Inline documentation must explain the *why* (rationale and edge cases), not merely restate the code.
- Avoid duplicate documentation files; code types and inline docstrings are the primary source of truth.

---

## 8. Technology Decisions (ADR)

These decisions were made deliberately; do not revert them to a single-language stack without revisiting the rationale.

### ADR-1: Layered Hybrid Architecture (Rust Actuation + Python Orchestration)

- **Actuation layer → Rust micro-driver, isolated process.** Mouse/keyboard/capture call into core-level macOS APIs (Quartz Event Tap, CGDisplay) that can lock up the whole system when they crash. Python's GIL and exception model give the wrong isolation for this layer. The driver is a small Rust binary speaking schema-validated JSON-RPC over a Unix socket; if it dies, the orchestrator survives and restarts it — a bounded respawn with backoff (`orchestrator/supervisor.py`), so a driver that cannot survive three spawns stops the run with its reason rather than being restarted forever (Law 2 self-correction at the process level). Rust's compile-time typing makes Law 6 real in this layer.
- **Orchestration layer → Python 3.12 stays.** Weak-model scaffolding, Pydantic contracts, the OODA loop, and skill distillation need fast iteration plus the LLM/VLM ecosystem. The bottleneck is the LLM turn (seconds), not actuation (milliseconds), so Python's overhead is irrelevant here. `pyright --strict` + Pydantic v2 + functional core satisfies Law 6 at this layer.
- **Consequence:** the actuation driver must never be imported as a Python module; it is always a separate process behind the RPC contract.

### ADR-2: Accessibility-First Grounding, Pixels as Verifier

- **Primary localization source = macOS Accessibility API (AXUIElement)**: exact coordinates + role + state + focus per element, stable across DPI/theme changes. This is the same API VoiceOver uses — still real-host, real-app interaction, not a synthetic bypass (Law 1 holds). Focus state gives the model feedback on whether a click landed, without needing screen-capture consent.
- **Screenshots/OCR/visual diff = verification**, not generation: candidate coordinates from the AX tree are confirmed by a regional visual diff before acting (`--verify`). Pixel-first OCR grounding is *specified* as the fallback for apps with no AX tree (games, some Electron apps), with VLM-based grounding after that — **this fallback is not implemented yet**, and until it is, a window with no AX tree yields no marks and the model must read coordinates off the screenshot. If the system-wide AX probe fails, the driver falls back to `CGWindowListCopyWindowInfo` (no consent required) to name the frontmost app, then grounds through that app's per-app AX tree.
- **Rationale:** OCR breaks on every DPI/theme/text change; AX does not. Verification by pixels keeps the Law 1 visual-grounding guarantee.

### ADR-3: Concrete Technology Stack (resolved — do not re-litigate)

| Layer | Choice | Rationale |
|---|---|---|
| Package manager | `uv` | Single tool for env + deps + scripts; fastest resolution |
| Python version | 3.12+ | Type hints, `X | None` unions, modern stdlib |
| Typing | `pyright --strict` | ADR-1 requirement; zero-error gate in CI |
| Lint/format | `ruff` (curated default rule set) | Import sorting, pyflakes, pyupgrade — dev extra, CI gate |
| Schemas | Pydantic v2 | Discriminated unions for the action contract |
| LLM transport | stdlib `urllib` transport in `providers/openai.py` (OpenAI-compatible) | Model-agnostic seam; `OPENAI_API_KEY` from env; no `openai` SDK dependency |
| Test runner | `pytest` | Smoke tests drive the real driver over a Unix socket |
| Rust driver | `core-graphics`, `core-foundation`, `objc2-app-kit` (macOS-gated) | Low-level OS APIs for actuation + AppKit indicator |

---

## 9. Agent Verification & Quality Checklist

Before completing any task or pull request in this repository, the agent must verify:
1. [ ] **Strict Typing Passed**: `pyright` passes without errors or warnings; `cargo clippy -D warnings` is clean; `ruff check` is clean.
2. [ ] **Pure Logic Tested**: Pure data transformations and skill distillers are validated with integration/smoke tests.
3. [ ] **Real Environment Compatibility**: Physical coordinate mappings account for macOS Retina scaling and display offsets.
4. [ ] **Context Budget Optimized**: Skills adhere to the two-stage summary/detail lazy-loading protocol.
5. [ ] **Safety & Permission Enforced**: Autonomy levels and dangerous action guards are never bypassed.

---

## 10. Engineering Standards & Workflow

This repository is developed using a senior-engineering workflow.

Always:

1. Understand the existing architecture before modifying code.
2. Research uncertain or version-sensitive technical decisions.
3. Research uncertain or version-sensitive APIs against the actual dependency versions (registry sources, official docs, or available MCP tools such as Context7).
5. Prefer minimal, maintainable architectural changes.
6. Add regression tests for behavior changes.
7. Test failure paths and edge cases.
8. Run formatting, lint, typecheck, tests and build where applicable.
9. Perform an adversarial self-review before completion.
10. Never hide, weaken or disable tests to obtain a green result.
11. Never claim completion without evidence.

For non-trivial changes:

understand → research → plan → implement → test → review → fix → regression test → validate

A green test suite alone is not proof of correctness.

