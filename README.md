# Freebuff Computer-Use Framework

An **autonomous, human-centric computer-use system** that operates directly on
the physical host — perceiving pixels, moving the real cursor along human-like
trajectories, and driving native desktop applications (real Dock icons, real
browser profiles, real OS dialogs), never a headless/sandboxed bypass.

The design thesis is **Prompt & Orchestration Supremacy**: the scaffolding —
strict JSON contracts, the OODA loop, validation gates, and self-correction —
must be so resilient that even a weak LLM stays reliable. The full project
constitution lives in [`AGENTS.md`](AGENTS.md); the two architectural pivots are
recorded there as ADR-1 and ADR-2:

## ADR-1 — Layered hybrid (Rust actuation + Python orchestration)

- **Python 3.12** owns orchestration: OODA loop, Pydantic contracts, skill
  distillation, memory. The bottleneck is the LLM turn (seconds), so Python's
  runtime cost is irrelevant here.
- **Rust** owns actuation as a **separate process** speaking typed JSON-RPC
  over a Unix socket. Python never imports the driver — if a CGEvent tap hangs
  the OS layer, only the driver process dies, and the orchestrator restarts it.

## ADR-2 — Accessibility-first grounding, pixels as verifier

- Primary localization = macOS Accessibility API (exact per-element
  coordinates/role/state, stable across DPI/theme).
- Screenshots/OCR/vision-diff **verify** candidate coordinates before acting.
  Pixel-first OCR is only the fallback for apps with no AX tree.

## Repository map

```
src/computeruse/
├── agent.py       # top-level composition: driver + sensor + guard + memory + skills
├── cli.py         # `python -m computeruse` — spawns driver, runs one goal
├── orchestrator/
│   ├── schemas.py   # 9 Pydantic action contracts (discriminated union)
│   ├── loop.py      # OODA: decide_step + OBSERVE/ORIENT + DISTILL (on_complete)
│   ├── prompts.py   # Law 2.1: weak-model scaffolding (prompt + parse + retry)
│   └── client.py    # typed JSON-RPC client to the Rust driver
├── skills/
│   ├── schemas.py   # SkillSummary (Stage 1) + SkillDefinition (Stage 2)
│   ├── registry.py  # two-stage search/load over the on-disk store
│   └── distiller.py # trajectory -> skill, signature-based dedup (semantic-param aware)
├── memory/
│   ├── schemas.py    # Law 4: Episode schema (trace + outcome + retrospective)
│   ├── episodic.py   # EpisodicStore; known_signatures feeds the distiller
│   └── semantic.py   # Law 4.2: SemanticStore (app knowledge) + pure search
├── security/
│   ├── killswitch.py # Law 5.2: kill-switch (shake detector + OODA gate)
│   └── autonomy.py   # Law 5.1: Level 0-3 guard, destructive-action detection
└── vision/
    ├── ax.py          # ADR-2 primary: AXElement tree + find_elements grounding
    ├── coordinates.py # ADR-2: pure retina/DPI scale + multi-display mapping
    ├── diff.py        # ADR-2: regional visual-diff core (anti-aliasing-safe)
    └── capture.py     # ADR-2: driver response -> ScreenCapture + BGRA->luma
driver/              # Rust actuation micro-driver (Unix-socket JSON-RPC)
                     #   backend.rs: Backend trait + SimulatedBackend (+capture, +ax)
                     #   ax.rs     : real macOS AXUIElement tree traversal (ADR-2)
                     #   quartz.rs : real macOS CGEvent backend + CGDisplay capture
                     #   bezier.rs : pure cubic-Bezier trajectory planning
tests/
├── smoke/           # contract-drift tests driving the real compiled driver
└── (unit/)          # pure data transformation tests
```

## Contract guarantees

The wire contract is handwritten on **both** sides (Python Pydantic vs Rust
`protocol.rs`), so `tests/smoke/test_contract_drift.py` drives every physical
action Python can produce through the *real* compiled driver and asserts an
`ack`. If the two schemas drift, the suite fails at runtime instead of silently
in production.

## Running

```bash
# Rust driver (default = simulated backend, safe for dev/CI)
cd driver && cargo build && cargo test

# Run the whole agent on one goal (demo provider: 2 clicks + finish)
uv run python -m computeruse --goal "open the export menu" --app Safari \
    --driver driver/target/debug/actuation-driver --store ~/.computeruse

# Real host actuation on macOS (requires Accessibility + Screen Recording)
./driver/target/debug/actuation-driver /tmp/actuation-driver.sock --real
# OpenAI transport (default model gpt-5.6-terra; key from OPENAI_API_KEY)
export OPENAI_API_KEY=sk-...
uv run python -m computeruse --goal "..." --real --driver driver/target/debug/actuation-driver \
    --verify --model openai            # or openai:gpt-5.6-luna / openai:gpt-5.6-sol
# ... a raw-text model of your own (module:callable, scaffolded):
uv run python -m computeruse --goal "..." --model my_module:my_model
# ... or bring your own state->AgentTurn provider:
uv run python -m computeruse --goal "..." --provider my_provider:make_provider

# Python type-checks (strict) and tests
uv sync --extra dev
uv run pyright src/computeruse
uv run pytest
```

The CLI spawns the driver itself (removing stale sockets), wires the autonomy
guard, a Ctrl-C kill-switch, visual verification (opt-in — the simulated
driver cannot render), and distills a skill + records an episode from every
successful run.

**Menu-bar chat launcher (macOS):** instead of a terminal you can run a tiny
status-bar app that drops a Liquid-Glass chat panel when clicked — type a goal
and it runs the agent, streaming live output back into the panel.

```bash
driver/target/debug/actuation-menu
```

It needs the agent model key without a shell, so it reads `OPENAI_API_KEY`
from its own environment or from `~/.computeruse/env`:

```bash
mkdir -p ~/.computeruse && echo 'OPENAI_API_KEY=sk-...' > ~/.computeruse/env
```

The launcher spawns the same CLI (`uv run python -m computeruse --real`) as a
subprocess; the target app is auto-detected from whatever was frontmost when
you opened the panel (or set one explicitly with the `app:` field), and the
driver keeps showing the translucent cursor halo — with a single menu-bar
icon (the spawned driver runs halo-only).

**Human presence & kinematics (Law 1, Law 5.2):** mouse movements are cubic
Beziers with distance-adaptive duration (a long sweep is never a teleport),
clicks carry a natural post-click pause, and typing follows a cadence. While
the driver runs under `--real` on macOS, a sea-blue status icon appears in the
menu bar and a translucent sea-blue halo follows the cursor — so the user
always sees where the agent is acting. Kill-switch: Command+Shift+Escape, or
just grab the mouse.

The smoke tests spawn the compiled driver (simulated backend) automatically,
and skip if the binary isn't built, so pure-check workflows stay green.
Never run those tests with `--real` — a real mouse in CI would be dangerous.

## Status

Working & tested:
- Orchestration spine: OODA loop (`decide_step`/`OodaRunner`), typed JSON-RPC
  client, contract-drift smoke tests against the real driver.
- Law 3 skills: distiller (trajectory -> definition + signature dedup) and
  two-stage registry (summary scan, lazy full load).
- Law 1 actuation: a `SimulatedBackend` (default) plus a real macOS
  `QuartzBackend` (`--real`) mapping the same trajectory interface onto CGEvent
  mouse/keyboard/scroll/type — so the pure Bezier planner is unit-tested and
  the physical connector is interchangeable. Distance-adaptive movement
  duration, post-click pauses, and `mouse_drag` carries `duration_ms`.
- Law 2 self-correction: a stuck-loop guard aborts a run after 5 identical
  actions with no progress (corrective error injected at the 3rd), and
  `max_steps` ends a run loudly instead of silently.
- Law 5.2 visibility: an AppKit menu-bar status icon + translucent sea-blue
  cursor halo while the real driver is active; app activation (`--app` brings
  the target to the front).
- Law 5 kill-switch: a pure mouse-shake detector + an `OodaRunner` gate that
  raises `KillSwitchTripped` the instant a human reclaims control.
- ADR-2 coordinate core: pure retina/DPI scaling and multi-display mapping in
  `vision/coordinates.py`, fully unit-tested without a display.
- ADR-2 visual-diff core: `vision/diff.py` implements an anti-aliasing-safe,
  downsample-then-compare regional diff (mean + moved-fraction signals) that
  feeds the OODA ORIENT step with an "unchanged / changed / noise" verdict.
- ADR-2 capture connector: the driver's `screenshot` RPC returns a typed
  BGRA8 frame (real `CGDisplayCreateImage` in Quartz, deterministic
  checkerboard in simulation, Screen-Recording-consent gated) that
  `vision/capture.py` decodes to luma — OODA OBSERVE finally has a sensor,
  and the global-point → display-px → pixel-luma mapping is tested end to end.
- Law 2 visual self-correction: `OodaRunner` accepts a `sensor` and the ORIENT
  step captures before/after every click, diffs the target region, and folds
  `VisualVerificationFailedError` (with mean/changed-fraction diagnostics)
  into `last_error` — an ACKed click that landed on nothing is now caught and
  surfaced to the next provider turn, without polluting `completed_steps`.
- Law 3 RETRIEVE wiring (OODA step 3): `OodaRunner` takes `skill_scan`
  (Stage 1: ranked summaries for a query) and `skill_loader` (Stage 2: full
  definition by id). Each turn it scans with the goal and mounts the top
  *same-app* match into the provider context under "Mounted skill:" — and a
  provider can swap it explicitly via a `load_skill` action. The agent wires
  both to the (now cached) `SkillRegistry`, so a known workflow is followed
  instead of re-derived; a failed scan/load degrades with a warning, never
  aborts.
- Law 3+4 DISTILL wiring: the runner records the *typed* executed trajectory
  and fires `on_complete(trajectory, outcome)` on every terminal `finish`.
  The caller wires it to `episode_from_trace` + `EpisodicStore` (Law 4) and
  `distill(...)` (Law 3) — the integration test proves the loop: a successful
  run is remembered and distilled, and a re-run of the same flow is rejected
  as `duplicate` via its episode signature. Aborted/kill-switched runs never
  distill a truncated trace.
- Top-level `agent.py` + `cli.py`: the product shell. One command composes
  driver client (ADR-1), visual sensor (ADR-2), autonomy guard (Law 5.1),
  live Ctrl-C kill-switch (Law 5.2, via `signal_predicate`), episodic memory
  and skill distillation — `python -m computeruse --goal ...` runs the demo
  provider against the simulated driver, distills a skill, records an
  episode, and the subprocess test + a live run verify the whole chain.
- Law 4.2 semantic memory: `memory/semantic.py` stores typed app knowledge
  (UI patterns, preferences, shortcuts, coordinate maps) with pure token-based
  retrieval, app scoping, and the same no-clobber disk layout as episodes.
  `Agent` RETRIEVEs the app's knowledge into the OODA working context as
  compact `[app] key: value` strings the provider sees every turn — the
  end-to-end test proves a seeded shortcut reaches the provider verbatim.
- ADR-2 accessibility grounding: the driver's `ax_snapshot` RPC walks an
  app's AXUIElement tree (roles/titles/positions in the global logical space)
  — real Quartz traversal behind Accessibility consent, deterministic Safari
  fixture in simulation. `vision/ax.py` parses it into typed `AXElement`
  trees, `find_elements` runs the grounding query ("find the Reload button"),
  and `element_rect` bridges into the coordinate layer; the end-to-end test
  maps an AX element's center to a display pixel. ADR-2's *primary* source
  (AX generates) now sits beside its verifier (pixels confirm).
- ADR-2 grounding into the loop: `OodaRunner` accepts an `ax_probe` and folds
  compact one-line summaries of the app's actionable elements (e.g.
  `Button "Reload" at (232,68) 44x24`) into the provider state before *every*
  decision, rendered under "UI elements on screen:" in the prompt — so a
  model's coordinates come from real AX elements instead of imagination, and
  pixels still verify whatever it picks. Summaries carry the element's
  **focus state** too — `TextField "..." at (158,90) 1164x24 (focused)` — so
  after clicking a field the *next* snapshot reports it focused: a
  consent-free "the click landed" confirmation the provider can act on
  without Screen Recording (verified live on Chrome's omnibox).
  `interactive_summaries` keeps the context minimal (actionable roles only,
  depth 8 so deep trees like Chrome's omnibox — five levels down — are not
  silently de-grounded, bounded by a 24-element count cap); a failed probe
  degrades to the previous context with a warning, never aborts. The
  capstone test drives the loop from a provider that reads the Reload
  button's center off the summaries and clicks it — the full ADR-2 chain:
  AX generates -> provider consumes -> pixels verify.
- ADR-2 focused-window perception: the driver's `focused_window` RPC returns
  the frontmost app (pid + name), its focused window's title, and the cursor
  position — the two non-pixel signals §5's OBSERVE step requires. Real
  Quartz reads the system-wide AX element + a probe CGEvent; simulation
  serves a deterministic Safari fixture. `vision/focus.py` validates it into
  a typed `FocusedWindow`, `OodaRunner` refreshes a compact summary into the
  provider state before *every* decision (best-effort: a failed probe logs
  once per run and degrades, never aborts — a permanently broken probe does
  not spam one line per step), and `Agent`/CLI auto-discover the frontmost
  app when none is named — the system knows what it is looking at without
  being told, and the discovered pid feeds `ax_snapshot` for the same app.
  Resilience: when the system-wide AX focused-app query fails (e.g. the
  frontmost app answers `kAXErrorCannotComplete`, or consent is missing),
  the driver falls back to `CGWindowListCopyWindowInfo`, which names the
  frontmost window's owner (pid + app name) with *no* Accessibility consent
  — perception survives a flaky primary (verified live on a host where the
  AX primary fails and the fallback still resolves the real frontmost app
  with a 216-element grounded tree).
- App activation as an OBSERVE precondition: a run launched from a terminal
  would otherwise ground against the terminal (frontmost when the CLI
  starts), not the app the goal means. The driver's `activate_app` RPC
  (`open -a` via LaunchServices — no Accessibility consent needed, the
  user's real app, never a synthetic bypass) brings the named app forward;
  `AgentConfig.activate_app_on_start` (CLI: `--app NAME` + `--real`) calls
  it before the first probe, so OBSERVE sees the intended app. An explicit
  name that cannot be resolved aborts cleanly with a hint to use the full
  Dock name (e.g. 'Google Chrome', not 'Chrome'); an auto-discovered app is
  never activated (it is already frontmost by definition).
- Bounded termination & stuck-loop guard (Law 2): the loop only ends when the
  provider emits `finish`, so a lost model must not be able to click forever.
  After 3 consecutive identical physical actions (click/drag/scroll/type/
  hotkey; `mouse_move` is deliberately excluded as ordinary cursor
  positioning) the runner folds a corrective hint into `last_error` telling
  the model to either `finish` or act differently; the action that would be
  the 5th repeat is refused and the run raises `StuckLoopError` before it
  reaches the physical layer. Exhausting `max_steps` now raises
  `MaxStepsError` (a truncated run is a typed failure, never a silent stop
  and never a distilled skill). The scaffold prompt additionally instructs
  the model to emit `finish` the moment the goal is achieved. Every exit is
  loud: `stuck loop:` / `max steps:` / `interrupted:` / `driver error:`.
- Law 2.1 weak-model scaffolding: `orchestrator/prompts.py` builds the full
  prompt from working state (goal, completed steps, injected `last_error`,
  semantic knowledge, the action contract), parses the model's raw text into
  a validated `AgentTurn` through the Pydantic gate, and `scaffolded_provider`
  re-prompts with corrective hints on invalid JSON — bounded retries, then
  the failure folds into `last_error`. `--model module:fn` exposes it in the
  CLI: a plain `str -> str` callable becomes a well-behaved provider. A
  subprocess test drives the whole stack from a raw-text fake model.
- OpenAI transport: `providers/openai.py` plugs into the `--model` seam as a
  plain `prompt -> text` callable (Chat Completions + strict `json_object`
  output; key from `OPENAI_API_KEY`, never committed). `--model openai` uses
  the balanced `gpt-5.6-terra` tier by default — the cost/quality sweet spot
  for a per-step JSON decision loop (the flagship sol is 2x the price, luna
  risks hallucinations on a physical host) — and `openai:<model-id>` overrides
  it. The transport is fully testable offline via an injected HTTP layer, and
  a faked-endpoint test drives the entire OODA loop end to end.
- Law 4 episodic memory: `memory/` persists every terminal run (trace + outcome
  + retrospective) and exposes `known_signatures()` so a repeated workflow is
  never re-distilled — Law 4 memory feeds Law 3 skills through the same
  flow-signature contract.- Law 5.1 autonomy guard: `security/autonomy.py` classifies actions by risk
  and maps Level 0-3 to allow/confirm/block; wired into the OODA VALIDATE step
  so a destructive move raises before ever touching the physical driver.
- Law 5.2 global kill-hotkey: the real driver installs a CGEventTap listening
  for Command+Shift+Escape (the event is *consumed*, never delivered to apps)
  and the orchestrator polls it via the `hotkey_state` RPC before every step.
  `KillSwitch.with_signal_predicate` OR-composes channels, so the agent wires
  the driver hotkey poll alongside the CLI's SIGINT catcher (or any caller's
  own switch) — a statically tripped switch cannot gain a live source (G2).
  The matching rule is pinned by Rust unit tests; the simulated driver never
  installs a tap (Law 1: no host interaction). All three Law 5.2 channels —
  global hotkey, rapid mouse shake, Ctrl-C — are now real.
- v2 adversarial-audit hardening: internal-action handlers narrow through the
  union with `isinstance` (no `getattr` bypass); the kill-switch rejects
  conflicting signal sources (`signal_triggered` vs `signal_predicate`); the
  diff `verdict()` honours `kind` for the noise call-out too (single-signal
  modes decide on their own signal); `known_signatures()` reads only each
  file's signature field; the shake monitor's window is a bounded `deque`;
  `SkillRegistry` caches its summary index (invalidated on save); and drags
  follow the same Bezier trajectory plan as moves — every finding pinned by
  a regression test.
**Not yet implemented:** nothing structural — the 6 laws, the 8 OODA steps,
all three memory tiers, both ADR-2 grounding halves, and the three Law 5.2
kill channels are implemented and tested. Natural next frontiers: vision
input to the model (feeding real screenshots into the prompt via a
multimodal-capable model), multi-agent orchestration, and a fine-tuned
skill-following model.
