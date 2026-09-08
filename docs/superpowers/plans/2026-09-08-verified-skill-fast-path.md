# Verified Skill Fast-Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a conservative verified-skill execution tier that can complete eligible repeated workflows with zero model decision turns while preserving the existing OODA permission, kill-switch, staleness, verification, recovery, and completion-audit gates.

**Architecture:** Keep `OodaRunner` as the only physical execution pipeline. Add typed, coordinate-free fast-path instructions to distilled skills and a deterministic provider wrapper that emits those instructions before the real provider. Semantic click instructions are resolved against the current AX summaries on every turn; any mismatch or execution error permanently disables the fast path for that run and delegates to the normal provider.

**Tech Stack:** Python 3.12, Pydantic v2, existing `OodaRunner`, existing AX summaries/verification pipeline, pytest, Ruff, Pyright, Rust CI gates.

**Spec:** `docs/superpowers/specs/2026-09-07-sovereign-memory-streaming-ui-design.md` §7

## Global Constraints

- No raw coordinate or stored mark-index replay.
- No second physical execution engine.
- Fast-path execution must pass through the same permission, kill-switch, focus/staleness, coordinate bounds, verification, recovery, and completion-audit machinery as normal OODA.
- Unsupported, ambiguous, dynamic, or environment-mismatched skills fall back to normal OODA instead of guessing.
- A forced/unverified completion never increases skill confidence.
- A/B evidence must count actual fallback-provider calls, not simulated token savings.

---

### Task 1: Add typed fast-path and confidence metadata

**Files:**
- Modify: `src/computeruse/skills/schemas.py`
- Modify: `src/computeruse/skills/registry.py`
- Test: `tests/smoke/test_skills.py`

**Interfaces:**
- Produces `FastPathAxPress`, `FastPathHotkey`, `FastPathActivateApp`, `FastPathWait`, and discriminated `FastPathInstruction`.
- Extends `SkillDefinition` and `SkillSummary` with `consecutive_successes`, `last_successful_at`, `last_environment`, `last_failure_reason`, and `fast_path`/`fast_path_ready` metadata.
- Produces `environment_fingerprint(app: str, goal: str) -> str` and `fast_path_eligible(summary: SkillSummary, *, app: str, goal: str) -> bool`.

- [ ] **Step 1: Write RED schema/eligibility tests**

Add tests proving:

```python
summary = SkillSummary(
    skill_id="safari.save",
    description="Save the document",
    app="Safari",
    uses=3,
    wins=3,
    consecutive_successes=3,
    last_environment=environment_fingerprint("Safari", "Save the document"),
    fast_path_ready=True,
)
assert fast_path_eligible(summary, app="Safari", goal="Save the document") is True
assert fast_path_eligible(summary, app="Safari", goal="Delete the document") is False
assert fast_path_eligible(summary.model_copy(update={"consecutive_successes": 1}), app="Safari", goal="Save the document") is False
```

Also assert the new metadata survives `summary_of()` and `instantiate_skill()`.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run pytest -q tests/smoke/test_skills.py
```

Expected: failures for missing fast-path types/metadata/functions.

- [ ] **Step 3: Implement minimal typed metadata**

Use coordinate-free instructions only:

```python
class FastPathAxPress(BaseModel):
    type: Literal["ax_press"]
    target: str = Field(min_length=1)

class FastPathHotkey(BaseModel):
    type: Literal["press_hotkey"]
    modifiers: tuple[Modifier, ...] = ()
    key: str = Field(min_length=1)

class FastPathActivateApp(BaseModel):
    type: Literal["activate_app"]
    app: str = Field(min_length=1)

class FastPathWait(BaseModel):
    type: Literal["wait"]
    duration_ms: int = Field(ge=0, le=5000)
    reason: str = "skill fast-path settle"
```

Eligibility v1 requires all of:
- `fast_path_ready`
- exact case-folded goal/description match
- exact app match
- at least 2 uses, at least 2 wins, `wins == uses`
- at least 2 consecutive successes
- current `environment_fingerprint(app, goal)` equals `last_environment`

`environment_fingerprint` is deterministic app + canonical site markers from the goal. It contains no machine secrets and no raw screen content.

- [ ] **Step 4: Extend outcome recording without weakening failure accounting**

Change:

```python
def record_outcome(
    self,
    skill_id: str,
    *,
    succeeded: bool,
    environment: str | None = None,
    failure_reason: str | None = None,
    observed_at: datetime | None = None,
) -> None:
```

On success increment `uses`, `wins`, `consecutive_successes`, write `last_successful_at`, and update `last_environment` when supplied. On failure increment `uses`, reset `consecutive_successes=0`, and persist bounded `last_failure_reason`.

- [ ] **Step 5: Re-run focused tests**

```bash
uv run pytest -q tests/smoke/test_skills.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/computeruse/skills/schemas.py src/computeruse/skills/registry.py tests/smoke/test_skills.py
git commit -m "feat(skills): track fast-path confidence"
```

---

### Task 2: Distill only replay-safe semantic instructions

**Files:**
- Modify: `src/computeruse/skills/distiller.py`
- Test: `tests/smoke/test_distill_loop.py`
- Test: `tests/smoke/test_parametric_skills_and_memory.py`

**Interfaces:**
- Produces `fast_path_from_trajectory(trajectory: Trajectory) -> tuple[FastPathInstruction, ...]`.
- `distill()` stores the returned tuple on `SkillDefinition.fast_path`.

- [ ] **Step 1: Write RED distillation tests**

Cover these cases:

1. `MouseClick`/`ClickMark` with a non-empty recorded `step_target` and left single click becomes `FastPathAxPress(target=...)`.
2. Click without a semantic target makes the **entire** fast path empty.
3. Right/double click, mouse move/drag/scroll, `TypeText`, `ClipboardPaste`, `CallTool`, `WebSearch`, `WebFetch`, and `LoadSkill` make the entire fast path empty in v1.
4. `PressHotkey`, `ActivateApp`, and bounded `Wait` are preserved as typed instructions.
5. `Finish` is not stored; the wrapper emits its own success claim only after every stored instruction completed.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run pytest -q tests/smoke/test_distill_loop.py tests/smoke/test_parametric_skills_and_memory.py
```

- [ ] **Step 3: Implement pure conversion**

The conversion is all-or-nothing. Pseudocode:

```python
for index, action in enumerate(trajectory.steps):
    if isinstance(action, Finish):
        continue
    if isinstance(action, (MouseClick, ClickMark)):
        target = trajectory.step_targets[index] if index < len(trajectory.step_targets) else ""
        if not target or action.button != "left" or action.click_count != 1:
            return ()
        instructions.append(FastPathAxPress(type="ax_press", target=target))
        continue
    if isinstance(action, PressHotkey): ...
    elif isinstance(action, ActivateApp): ...
    elif isinstance(action, Wait): ...
    else:
        return ()
```

No coordinate values are ever copied.

- [ ] **Step 4: Re-run focused tests and commit**

```bash
uv run pytest -q tests/smoke/test_distill_loop.py tests/smoke/test_parametric_skills_and_memory.py
git add src/computeruse/skills/distiller.py tests/smoke/test_distill_loop.py tests/smoke/test_parametric_skills_and_memory.py
git commit -m "feat(skills): distill semantic fast-path instructions"
```

---

### Task 3: Add deterministic provider wrapper with fail-open-to-OODA semantics

**Files:**
- Create: `src/computeruse/skills/fast_path.py`
- Test: `tests/smoke/test_verified_skill_fast_path.py`

**Interfaces:**
- Produces `FastPathProvider`, callable as `Callable[[WorkingState], AgentTurn]`.
- Produces pure `resolve_ax_target(target: str, ui_elements: tuple[str, ...]) -> MouseClick | None`.
- Exposes `provider_calls`/`fast_path_turns` counters for A/B evidence.

- [ ] **Step 1: Write RED target-resolution tests**

Use real AX summary syntax, for example:

```python
ui = (
    'Button "Cancel" at (100,200) 80x24',
    'Button "Save" at (220,200) 80x24',
)
action = resolve_ax_target('Button "Save"', ui)
assert action == MouseClick(type="mouse_click", x=260, y=212)
```

Require exact normalized identity match. Ambiguous duplicate matches return `None`. Missing targets return `None`. No fuzzy title guessing.

- [ ] **Step 2: Write RED wrapper tests**

Prove:
- eligible semantic steps are emitted without calling the fallback provider;
- after all steps, wrapper emits `Finish(status="success")` without a provider call;
- `state.last_error` disables the fast path permanently and delegates that turn to fallback;
- target missing/ambiguous disables the fast path and delegates immediately;
- fallback provider is never called before a mismatch/error;
- counters distinguish fast-path turns from real provider calls.

- [ ] **Step 3: Implement wrapper**

`FastPathProvider.__call__` rules:

```python
if self._disabled or state.last_error is not None:
    self._disabled = True
    return self._call_fallback(state)

instruction = self._next_instruction()
if instruction is None:
    self.fast_path_turns += 1
    return AgentTurn(... Finish(status="success", summary="verified skill fast-path completed"))

turn = instruction_to_turn(instruction, state.ui_elements)
if turn is None:
    self._disabled = True
    return self._call_fallback(state)
self.fast_path_turns += 1
return turn
```

Do not mutate `WorkingState`; wrapper state is only its own cursor/disabled/counters.

- [ ] **Step 4: Run tests and commit**

```bash
uv run pytest -q tests/smoke/test_verified_skill_fast_path.py
git add src/computeruse/skills/fast_path.py tests/smoke/test_verified_skill_fast_path.py
git commit -m "feat(skills): add verified fast-path provider"
```

---

### Task 4: Integrate eligibility into Agent and prove real provider-turn reduction

**Files:**
- Modify: `src/computeruse/agent.py`
- Modify: `tests/smoke/test_skill_acceleration_ab.py`
- Modify: `tests/smoke/test_distill_loop.py`
- Test: `tests/smoke/test_verified_skill_fast_path.py`

**Interfaces:**
- Agent selects at most one exact-goal fast-path candidate before constructing `OodaRunner`.
- When eligible, it wraps `AgentConfig.provider` in `FastPathProvider`; otherwise it passes the original provider unchanged.
- Existing `OodaRunner` remains the sole runner.

- [ ] **Step 1: Write RED Agent integration tests**

Create a proven skill with a semantic button target, matching environment, and `consecutive_successes >= 2`. Run the agent with a fallback provider that increments a counter. Assert:
- the physical action still enters the normal runner/guard/verification path;
- the independent completion checker still runs;
- fallback provider calls are `0` on clean fast-path success;
- a target mismatch causes fallback calls `> 0` and does not execute a guessed coordinate;
- a guard denial does not bypass policy;
- a verification failure causes fallback instead of repeated blind replay.

- [ ] **Step 2: Pass environment/failure evidence into skill outcome recording**

At completion compute:

```python
environment = environment_fingerprint(app, self._config.goal)
```

Pass it to `record_outcome`. For failures use the terminal retrospective/last error as the bounded `failure_reason`. Keep the existing `verified = outcome == "success" and not forced_completion` rule authoritative.

- [ ] **Step 3: Replace the old simulated acceleration proof with actual call accounting**

Update `test_skill_acceleration_ab.py` so Pass 1 and Pass 2 count the real provider callable invocations. Clean reset rules remain. Warm pass must prove:
- target output is independently recreated;
- a verified skill is used;
- fallback/model provider calls are strictly lower than cold pass, and for the eligible fixture equal `0`;
- safety/audit path is still exercised;
- no stale artifact from Pass 1 exists before Pass 2.

Keep step/duration metrics, but do not claim token reduction from hard-coded token increments.

- [ ] **Step 4: Run focused suite**

```bash
uv run pytest -q \
  tests/smoke/test_skills.py \
  tests/smoke/test_distill_loop.py \
  tests/smoke/test_parametric_skills_and_memory.py \
  tests/smoke/test_verified_skill_fast_path.py \
  tests/smoke/test_skill_acceleration_ab.py
```

- [ ] **Step 5: Adversarial regression cases**

Add/verify tests for:
- same goal, different app;
- same app, different named site;
- one historical failure breaking the perfect-record eligibility requirement;
- duplicate AX target labels;
- absent AX target;
- stored right/double click;
- forced completion;
- destructive semantic target under Guarded/Full policy;
- fast-path failure followed by normal provider recovery.

- [ ] **Step 6: Full verification**

```bash
uv run ruff check .
uv run pyright
uv run pytest -q --tb=short
cargo test --manifest-path driver/Cargo.toml
cargo clippy --manifest-path driver/Cargo.toml --all-targets -- -D warnings
```

GitHub CI must additionally pass the macOS real-backend gate.

- [ ] **Step 7: PR evidence**

The PR body must report:
- RED failures before implementation;
- final pytest pass/skip count;
- Ruff/Pyright/Rust/macOS gate status;
- clean A/B cold vs warm provider-call counts and steps;
- confirmation that no coordinate/mark index is persisted or replayed;
- adversarial fallback/policy results.

- [ ] **Step 8: Squash merge only after fresh CI**

After merge, verify the fresh `main` CI for the squash commit before starting ActivityEvent work.
