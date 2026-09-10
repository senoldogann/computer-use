# PR D: Skill Fast Path (speed-first)

## 1. Problem (measured)

Real-host contact-form run, 2026-09-08: **12 LLM turns, 166,861 tokens,
$0.36, ~145 s in-run** for 19 physical actions. Per-turn cost, measured from
`phase_s`: observe 2.5–8.5 s (screenshot + AX capture) + decide 2.8–11.5 s
(LLM call on a 10–15k-token prompt) + act ~2 s (pixel verification). The loop
already supports multi-action batches and the prompt already encourages them,
yet the model filled 5 form fields one action per turn — including the
textbook Cmd+L → paste → Return sequence the prompt cites as its example.
Prompt adherence alone cannot be relied on for speed.

Speed is now the top operator priority. The cure is structural: stop paying
for model turns on workflows the store already knows.

## 2. Design

**Verified-skill replay (tier 2 of spec §7.1).** When a run mounts a skill
that (a) describes *exactly* this goal, (b) has a proven consecutive track
record, (c) was proven in *this* environment, and (d) carries a
machine-readable recipe, the loop replays the recipe with **zero provider
turns** and spends exactly **one** provider turn afterwards to verify the
goal and finish. Expected effect on the measured run class: 12 turns → ~2
(1 replay + 1 verify), tokens and wall clock cut by ~80%.

Non-negotiables (speed-first, but not safety-last):

- Every replayed step runs through the existing `_execute_one` pipeline:
  coordinate gate, permission guard, stuck-loop guard, VERIFY witnesses.
  The fast path skips *decisions*, never *gates*.
- No coordinate replay, ever. Recipe steps store the action payload minus
  coordinates plus the AX identity (`target_element_identity`); replay
  re-grounds each positional step against the live mark list
  (`mark_identity` match → `ClickMark` → existing resolution). A target
  that is not on screen ends the fast path, it never clicks blind.
- Replay only for the **exact goal** the skill was distilled from
  (`skill_for_goal` semantics). Operands (typed/pasted text) are stored
  because for an identical goal they belong to this task, not a past one.
  Parametric reuse across different goals is explicitly out of scope.
- First verification miss, first non-replayable step, or first missing
  target ends the fast path: record the failure reason on the skill
  (consecutive-wins reset), unmount, and continue as a normal OODA run.
  The provider then sees the skill mounted as usual plus the failure hint.
- `finish` is never replayed. Goal completion is always claimed by a
  provider turn and audited by the completion checker (Law 9 FINISH audit).

## 3. Contract changes

`skills/schemas.py`:

- `SkillDefinition` gains: `consecutive_wins: int = 0`,
  `last_success_at: datetime | None = None`, `last_env: str = ""`
  (`"{app}|{site}"`, site via existing `site_of_goal`),
  `last_failure_reason: str = ""`, `recipe: tuple[RecipeStep, ...] = ()`.
- `RecipeStep`: `{action_type: str, params: dict[str, JSON scalar], target:
  str}`. Params are the stored action payload **minus coordinate keys**;
  replay drops coordinate keys again defensively, injects freshly grounded
  ones, and rebuilds via existing `action_from_payload` (returns None →
  fast path ends, never a guess).
- Only physical actions + `wait` enter a recipe (`mouse_click`,
  `mouse_drag`, `mouse_move`, `mouse_scroll`, `type_text`,
  `clipboard_paste`, `press_hotkey`, `activate_app`). `CallTool` excluded:
  tool answers feed reasoning and cannot be replayed blind. `finish` /
  `load_skill` excluded by construction.
- `SkillSummary` projects `consecutive_wins` + a `has_recipe` bit (ranking
  stays over summaries; full recipe loads only in Stage 2).
- All new fields defaulted: old skill files on disk keep parsing.

`skills/distiller.py`: `distill()` builds `recipe` from the trajectory
(typed actions + positional `step_targets`, coordinates stripped). A
trajectory whose steps are all non-replayable yields an empty recipe —
still a valid skill, never fast-path eligible.

`skills/registry.py`:

- `record_outcome(skill_id, *, succeeded, env="", failure_reason="")`
  maintains `uses`/`wins` (unchanged) plus `consecutive_wins`
  (reset on failure), `last_success_at` + `last_env` (on success),
  `last_failure_reason` (on failure, bounded length).
- Pure `fast_path_eligible(definition, *, current_env) -> bool`:
  `consecutive_wins >= FAST_PATH_MIN_CONSECUTIVE_WINS (2)`,
  `recipe` non-empty, `last_env == current_env`, not demoted.
  Threshold is a named constant, not a tunable flag (repo rule: no
  multi-mode functions; one policy, tested).

`orchestrator/loop.py`:

- `_maybe_fast_path(state, goal)`: attempted at most once per run, before
  the first provider turn, only when `self._skill` is mounted. Checks, in
  order: eligibility, exact-goal match (`skill_for_goal` semantics against
  the mounted skill), then replays: fresh `_observe` per step (cheap, no
  LLM), re-ground positional targets to live marks, synthesize an
  `AgentTurn` (thought names the replay, sub-goal the skill step), feed
  through `_execute_one`. Any miss → record failure on the skill via the
  existing reinforcement path data, unmount, return None (normal OODA
  continues). Full replay → return updated state; the loop's next iteration
  does the single verify-and-finish provider turn.
- Fast-path steps trace exactly like normal steps (same routes — they
  *are* normal validated actions). The headline metric is provider-call
  count per run, asserted in tests, not a trace marker.

`agent.py`: `record_outcome` call site passes `env=f"{app}|{site}"` and the
run's failure reason (retrospective/last_error, bounded).

## 4. Deliberately deferred (§7.3 and beyond)

- Prompt diet (10–15k tokens/turn): risky without many real runs to
  regress against; revisit after fast-path measurement.
- Semantic-retrieval caching, AX/OCR dedup within a generation: no
  evidence they dominate (observe is 2.5–8.5 s vs decide 2.8–11.5 s);
  measure from `phase_s` before optimizing.
- Parametric recipe reuse (placeholders/slots): needs operand mapping the
  store cannot do honestly today.
- Tier 1 deterministic direct path: no local operation contracts exist
  yet; fast path covers the measured pain.

## 5. Acceptance

- Simulated-backend loop test: proven skill + exact goal → provider called
  **once** (verify/finish), all recipe actions executed and verified;
  assert turn count, not just outcome.
- Miss test: changed screen (target absent) → zero replayed actions,
  skill failure recorded (`consecutive_wins` reset), normal OODA completes
  the run.
- Confidence tests: `record_outcome` streak maintenance, env mismatch
  ineligibility, old skill files (no new fields) still load and are
  ineligible (empty recipe).
- Full gates green. Real-host validation (form rerun turn-count
  comparison) left to the operator: it spends real money.
