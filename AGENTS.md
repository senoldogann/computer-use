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
3. **Human-like Kinematics**: Mouse movements must follow natural, continuous trajectories (cubic Bezier curves with distance-adaptive duration, realistic velocity profiles, variable typing speeds, post-click micro-pauses) rather than instantaneous coordinate teleportation. While the agent runs, a translucent sea-blue halo follows the cursor and a menu-bar status item shows the agent is active (Law 5.2: the user always sees what the agent is doing).

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
1. **Episodic Memory**: Full trace of past sessions, successful task trajectories, and failure retrospectives saved in structured formats.
2. **Semantic Knowledge Store**: Searchable memory index containing application-specific UI patterns, user preferences, coordinate maps, and shortcut behaviors.
3. **Working Context (Scratchpad)**: Minimal, clean, rolling state tracking current goal, completed steps, pending sub-tasks, and latest visual diffs.

### Law 5: Explicit Permission Governance & Security Boundaries
1. **Configurable Autonomy Levels**:
   - **Level 0 (Observer / Advisory)**: Recommends actions and highlights UI regions without taking control.
   - **Level 1 (Supervised / Step-by-Step)**: Proposes each action and waits for user confirmation before physical execution.
   - **Level 2 (Guarded Autonomy)**: Executes routine actions autonomously; pauses and prompts user confirmation for destructive actions (e.g., file deletion, payments, terminal commands, email dispatch).
   - **Level 3 (Full Autonomy / Auto Mode)**: Unattended execution with continuous self-monitoring, safety boundary checks, and automatic fallback on anomalies.
2. **Emergency Kill-Switch**: The user must always be able to reclaim physical control instantly. Implemented channels: **Command+Shift+Escape** (global event-tap hotkey — the driver reports `hotkey_state` and aborts the current action), **SIGINT/SIGTERM** (Ctrl-C), and **rapid cursor shake** (fail-safe interrupt). When tripped, all queued actions are dropped and the run ends; the user can always just grab the mouse.

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
3. `mouse_drag`: `{"type": "mouse_drag", "start_x": int, "start_y": int, "end_x": int, "end_y": int, "duration_ms": int}` (Same Bezier kinematics as `mouse_move`).
4. `mouse_scroll`: `{"type": "mouse_scroll", "dx": int, "dy": int}` (Scrolls **at the current cursor position** — emit a `mouse_move` first when the target matters).
5. `type_text`: `{"type": "type_text", "text": str, "wpm": int}` (Types text with human-like inter-key delays).
6. `press_hotkey`: `{"type": "press_hotkey", "modifiers": ["command"|"shift"|"alt"|"control"], "key": str}`.
7. `load_skill`: `{"type": "load_skill", "skill_id": str}` (Explicitly mounts a skill, replacing the auto-retrieved one — see RETRIEVE).
8. `wait`: `{"type": "wait", "duration_ms": int, "reason": str}`.
9. `finish`: `{"type": "finish", "status": "success"|"failed", "summary": str}`.

---

## 5. Execution State Machine (OODA Loop)

Every agent step strictly executes the following cycle:

```
[1. OBSERVE]  ──► Automatic, orchestrator-driven: inject the focused app's AX snapshot
                  (element summaries + coordinates + focus state) into model context;
                  when visual verification is enabled, also capture a Retina-aware screenshot.
                  The model NEVER requests observation — it is injected before every decision.
       │
[2. ORIENT]   ──► Compare against the previous step: focus-state changes, AX tree diffs, and
                  (when enabled) pixel diff. If the last action failed, a structured last_error
                  is injected into the next decision. Failure semantics: the model may re-attempt
                  with corrected parameters, but the stuck-loop guard (Law 2.2) bounds repetition.
       │
[3. RETRIEVE] ──► Hybrid: the orchestrator scans the Skill Summary Index and auto-mounts the top
                  same-app match (zero extra model tokens). The model may override by emitting
                  load_skill for a different skill in DECIDE.
       │
[4. DECIDE]   ──► LLM generates structured Thought + Sub-goal + Single Action JSON.
       │
[5. VALIDATE] ──► Permission Guard verifies Autonomy Level & safety bounds (e.g. destructive action check).
       │
[6. ACTUATE]  ──► Physical Actuation Engine executes action with human-like kinematics.
       │
[7. (post-run) DISTILL] ──► After the run completes (finish or max_steps), analyze the trajectory
                            and synthesize a new reusable Skill if novel. A run interrupted by
                            max_steps or abort NEVER distills.
```

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
│       ├── indicator.rs       # Menu-bar status item + sea-blue cursor halo (AppKit, macOS only)
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
│   │   ├── loop.py            # OODA state machine: observe/orient/retrieve/decide/validate/actuate
│   │   ├── schemas.py         # Pydantic action contracts (discriminated unions, strict typing)
│   │   ├── prompts.py         # Scaffolding prompts & error-correction injectors
│   │   └── client.py          # Typed JSON-RPC client for the Rust driver (Unix socket)
│   ├── providers/
│   │   └── openai.py          # LLM transport (model-agnostic seam: OpenAI-compatible)
│   ├── vision/
│   │   ├── ax.py              # ADR-2: AX element summaries, focus state, count/depth caps
│   │   ├── coordinates.py     # Retina-to-virtual coordinate transformation
│   │   ├── capture.py         # Display capture (verification only)
│   │   ├── diff.py            # Pre/post action visual diffing
│   │   └── focus.py           # Focused-app discovery + activation
│   ├── skills/
│   │   ├── registry.py        # Summary-index & lazy-loading engine (two-stage retrieval)
│   │   ├── distiller.py       # Trajectory-to-skill distillation (pure, de-duplicating)
│   │   └── schemas.py         # SkillDefinition / SkillSummary types
│   ├── memory/
│   │   ├── episodic.py        # Past execution traces & retrospectives
│   │   └── semantic.py        # Key-value UI patterns & preferences
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
- Always raise explicit, domain-specific errors (e.g., `CoordinateOutOfBoundsError`, `VisualVerificationFailedError`, `PermissionDeniedError`).
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

- **Actuation layer → Rust micro-driver, isolated process.** Mouse/keyboard/capture call into core-level macOS APIs (Quartz Event Tap, CGDisplay) that can lock up the whole system when they crash. Python's GIL and exception model give the wrong isolation for this layer. The driver is a small Rust binary speaking schema-validated JSON-RPC over a Unix socket; if it dies, the orchestrator survives and restarts it (Law 2 self-correction at the process level). Rust's compile-time typing makes Law 6 real in this layer.
- **Orchestration layer → Python 3.12 stays.** Weak-model scaffolding, Pydantic contracts, the OODA loop, and skill distillation need fast iteration plus the LLM/VLM ecosystem. The bottleneck is the LLM turn (seconds), not actuation (milliseconds), so Python's overhead is irrelevant here. `pyright --strict` + Pydantic v2 + functional core satisfies Law 6 at this layer.
- **Consequence:** the actuation driver must never be imported as a Python module; it is always a separate process behind the RPC contract.

### ADR-2: Accessibility-First Grounding, Pixels as Verifier

- **Primary localization source = macOS Accessibility API (AXUIElement)**: exact coordinates + role + state + focus per element, stable across DPI/theme changes. This is the same API VoiceOver uses — still real-host, real-app interaction, not a synthetic bypass (Law 1 holds). Focus state gives the model feedback on whether a click landed, without needing screen-capture consent.
- **Screenshots/OCR/visual diff = verification**, not generation: candidate coordinates from the AX tree are confirmed by a regional visual diff before acting (`--verify`). Pixel-first OCR grounding is the fallback only for apps with no AX tree (games, some Electron apps), with VLM-based grounding after that. If the system-wide AX probe fails, the driver falls back to `CGWindowListCopyWindowInfo` (no consent required) to name the frontmost app, then grounds through that app's per-app AX tree.
- **Rationale:** OCR breaks on every DPI/theme/text change; AX does not. Verification by pixels keeps the Law 1 visual-grounding guarantee.

### ADR-3: Concrete Technology Stack (resolved — do not re-litigate)

| Layer | Choice | Rationale |
|---|---|---|
| Package manager | `uv` | Single tool for env + deps + scripts; fastest resolution |
| Python version | 3.12+ | Type hints, `X | None` unions, modern stdlib |
| Typing | `pyright --strict` | ADR-1 requirement; zero-error gate in CI |
| Lint/format | `ruff` (curated default rule set) | Import sorting, pyflakes, pyupgrade — dev extra, CI gate |
| Schemas | Pydantic v2 | Discriminated unions for the action contract |
| LLM transport | `openai` SDK (OpenAI-compatible) | Model-agnostic seam; `OPENAI_API_KEY` from env |
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

