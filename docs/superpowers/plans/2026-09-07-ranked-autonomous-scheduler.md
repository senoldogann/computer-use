# Ranked Autonomous Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace random autonomous memory retries with deterministic, provenance-bearing ranked work proposals while preserving inbox claim-once, mission resume, session budgets, and existing safety semantics.

**Architecture:** Introduce a small pure scheduling module that owns proposal provenance, cost estimation, utility scoring, and deterministic ranking. Keep `autonomous.py` responsible for unattended-session behavior and store-aware memory candidate collection, and keep `cli.py` responsible for claim/resume orchestration. Operator inbox work and resumable missions retain their current side-effect-safe acquisition paths, but every proposal uses the same typed provenance/scoring contract.

**Tech Stack:** Python 3.12, dataclasses, `typing.Literal`, existing `SkillRegistry`, `EpisodicStore`, `MissionStore`, `UsageStore`, pytest, Ruff, Pyright.

**Spec:** `docs/superpowers/specs/2026-09-07-sovereign-memory-streaming-ui-design.md` §6.1.

## Global Constraints

- No candidate may be invented from an unconstrained “do something useful” prompt; every autonomous goal must have concrete provenance.
- Candidate source order for this PR is: explicit operator inbox work, interrupted/failed missions, demoted skills worth repairing, failed episodes worth retrying, then low-confidence/unproven skill validation.
- Preference-backed recurring work stays out of this PR because the typed preference model belongs to PR C.
- `GoalProposal` must carry `source_type`, `source_id`, `utility_score`, `confidence`, `expected_cost`, and `reason` in addition to `goal` and `app`.
- Ranking must be deterministic for identical stored state. Do not use ambient randomness to choose among non-equal candidates.
- Source-priority bands must not be overturned by the cost/confidence adjustment; an explicit operator task always outranks a resumable mission, and a resumable mission always outranks memory-maintenance candidates.
- Inbox task acquisition remains atomic claim-once. Do not parse or reserve a task before the existing claim operation just to rank it.
- Mission resume must continue using `remaining_goal(mission)` and must never repeat already-completed physical sub-goals.
- Existing hard budgets, idle/reclaim behavior, kill switch, approval parking, Sovereign/FULL permission semantics, trace, verification, and attempt ceilings are unchanged.
- No new runtime dependency is allowed.

---

## File Map

- Create `src/computeruse/scheduler.py`: pure proposal schema, source taxonomy, cost estimate, utility score, deterministic rank helpers.
- Modify `src/computeruse/autonomous.py`: collect memory-backed candidates and delegate ranking to `scheduler.py`; retain unattended session loop.
- Modify `src/computeruse/cli.py`: enrich inbox and mission proposals with provenance/cost/score; read current usage history for each proposal cycle; remove random proposer selection.
- Modify `tests/smoke/test_autonomous.py`: preserve existing session behavior tests while adapting proposal fixtures to the richer contract.
- Create `tests/smoke/test_scheduler.py`: focused pure tests for provenance, scoring, deterministic ranking, and cost estimation.
- Modify existing folder-watch/mission tests only if a regression proves necessary; do not refactor unrelated CLI code.

---

### Task 1: Add the provenance-bearing proposal contract and pure ranker

**Files:**
- Create: `src/computeruse/scheduler.py`
- Create: `tests/smoke/test_scheduler.py`

**Interfaces:**
- Produces `ProposalSource = Literal["operator_inbox", "mission_resume", "skill_repair", "episode_retry", "skill_validation"]`.
- Produces `GoalProposal` dataclass with fields `goal`, `app`, `source_type`, `source_id`, `utility_score`, `confidence`, `expected_cost`, `reason`.
- Produces `proposal_score(source_type, confidence, expected_cost) -> float`.
- Produces `make_proposal(...) -> GoalProposal`.
- Produces `rank_proposals(proposals: tuple[GoalProposal, ...]) -> tuple[GoalProposal, ...]`.

- [ ] **Step 1: Write failing contract tests**

Add tests that instantiate a proposal and assert every provenance field is required and retained:

```python
from computeruse.scheduler import GoalProposal


def test_goal_proposal_carries_auditable_provenance() -> None:
    proposal = GoalProposal(
        goal="retry failed export",
        app="Finder",
        source_type="episode_retry",
        source_id="episode-42",
        utility_score=207.5,
        confidence=0.75,
        expected_cost=0.12,
        reason="episode episode-42 failed",
    )

    assert proposal.source_type == "episode_retry"
    assert proposal.source_id == "episode-42"
    assert proposal.confidence == 0.75
    assert proposal.expected_cost == 0.12
```

Also assert `confidence` outside `[0.0, 1.0]` is rejected by `make_proposal` with `ValueError`, and empty `goal` / `source_id` are rejected.

- [ ] **Step 2: Run the new scheduler tests RED**

Run:

```bash
uv run pytest -q tests/smoke/test_scheduler.py
```

Expected: collection/import failure because `computeruse.scheduler` does not exist.

- [ ] **Step 3: Implement the minimal pure contract**

Create `src/computeruse/scheduler.py` with these source priority bands:

```python
SOURCE_BASE_UTILITY: Final[dict[ProposalSource, float]] = {
    "operator_inbox": 500.0,
    "mission_resume": 400.0,
    "skill_repair": 300.0,
    "episode_retry": 200.0,
    "skill_validation": 100.0,
}
```

Use this score formula:

```python
confidence_bonus = confidence * 10.0
cost_penalty = min(max(expected_cost or 0.0, 0.0), 9.0)
score = SOURCE_BASE_UTILITY[source_type] + confidence_bonus - cost_penalty
```

The 100-point bands are deliberate: confidence/cost may order candidates inside one source class but can never invert the architecture’s source priority.

`rank_proposals` must sort by:

```python
(-proposal.utility_score, proposal.source_type, proposal.source_id, proposal.goal)
```

No RNG is used. Equal scores therefore have a stable provenance-based order.

- [ ] **Step 4: Run scheduler tests GREEN**

```bash
uv run ruff check src/computeruse/scheduler.py tests/smoke/test_scheduler.py
uv run pyright
uv run pytest -q tests/smoke/test_scheduler.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/computeruse/scheduler.py tests/smoke/test_scheduler.py
git commit -m "feat: add autonomous proposal ranking contract"
```

---

### Task 2: Estimate proposal cost from recorded run history

**Files:**
- Modify: `src/computeruse/scheduler.py`
- Modify: `tests/smoke/test_scheduler.py`

**Interfaces:**
- Consumes existing `computeruse.orchestrator.report.UsageRecord`.
- Produces `estimate_expected_cost(goal: str, usage: tuple[UsageRecord, ...]) -> float | None`.

- [ ] **Step 1: Write failing cost-estimation tests**

Construct usage records for two goals and assert:

```python
assert estimate_expected_cost("export report", records) == pytest.approx(0.30)
assert estimate_expected_cost("never seen", records) is None
```

The estimate is the arithmetic mean of `cost_usd` for records whose normalized goal matches exactly. Normalize only whitespace with `" ".join(goal.split())`; do not fuzzy-match unrelated tasks by app or keywords.

Add a regression proving zero-dollar records are valid data rather than silently discarded.

- [ ] **Step 2: Run the cost tests RED**

```bash
uv run pytest -q tests/smoke/test_scheduler.py -k expected_cost
```

Expected: FAIL because `estimate_expected_cost` is absent.

- [ ] **Step 3: Implement exact-goal historical cost estimation**

The function must:

1. normalize the requested goal,
2. select matching `UsageRecord.goal` values using the same normalization,
3. return `None` for no matches,
4. otherwise return `sum(cost_usd) / count`.

Do not create a model-based estimate or app-wide fallback in this PR. Unknown cost remains explicitly unknown.

- [ ] **Step 4: Run Task 2 GREEN**

```bash
uv run ruff check src/computeruse/scheduler.py tests/smoke/test_scheduler.py
uv run pyright
uv run pytest -q tests/smoke/test_scheduler.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/computeruse/scheduler.py tests/smoke/test_scheduler.py
git commit -m "feat: estimate autonomous work cost from history"
```

---

### Task 3: Replace random memory retry with ranked memory candidates

**Files:**
- Modify: `src/computeruse/autonomous.py`
- Modify: `tests/smoke/test_autonomous.py`
- Test: `tests/smoke/test_scheduler.py`

**Interfaces:**
- Consumes `make_proposal`, `rank_proposals`, `estimate_expected_cost`, `GoalProposal` from `computeruse.scheduler`.
- Changes `propose_goal` to accept current usage history and no ambient RNG:

```python
def propose_goal(
    skills: SkillRegistry,
    episodes: EpisodicStore,
    *,
    usage: tuple[UsageRecord, ...],
    exclude: Container[str],
) -> GoalProposal | None:
```

- Re-export `GoalProposal` from `computeruse.autonomous` during this PR so existing imports outside the scheduler do not break unnecessarily.

- [ ] **Step 1: Add RED ranking regressions**

Replace the old “random member of first non-empty pool” expectations with these assertions:

1. Same stores called twice produce the same proposal.
2. A demoted skill outranks a failed episode.
3. A failed episode outranks an unproven skill.
4. Two candidates in the same class are ordered by computed utility, then stable source id.
5. Exclusion removes candidates before ranking and never causes an early `None` while another candidate remains.
6. Empty stores still return `None`; no unconstrained goal is invented.

Add provenance assertions:

```python
assert proposal.source_type == "skill_repair"
assert proposal.source_id == "chrome.broken"
```

For memory candidates use confidence values:

- `skill_repair`: `min(1.0, summary.uses / 5.0)`
- `episode_retry`: `0.75`
- `skill_validation`: `0.50 + 0.10 * min(summary.uses, 1)`

These values express evidence strength, not permission. Permission is still handled later by the autonomy guard.

- [ ] **Step 2: Run autonomous scheduler tests RED**

```bash
uv run pytest -q tests/smoke/test_autonomous.py tests/smoke/test_scheduler.py -k "broken_skill or failed_episode or unproven or excluded or deterministic or provenance"
```

Expected: FAIL because current `propose_goal` returns from source pools early and uses `rng.choice`.

- [ ] **Step 3: Implement ranked memory candidate collection**

In `autonomous.py`:

- remove `random` from proposal selection,
- gather all eligible demoted skills, failed episodes, and unproven skills into one `list[GoalProposal]`,
- compute `expected_cost` from current usage for each goal,
- build each candidate through `make_proposal`,
- call `rank_proposals(tuple(candidates))`,
- return the first ranked proposal or `None`.

Reasons must retain the concrete source id and useful evidence counts. Do not place the numerical score into prose; it already exists in the typed proposal.

- [ ] **Step 4: Adapt unattended-session fixtures to the richer proposal type**

Add one test helper in `tests/smoke/test_autonomous.py`:

```python
def _proposal(goal: str) -> GoalProposal:
    return make_proposal(
        goal=goal,
        app=None,
        source_type="episode_retry",
        source_id=f"fixture-{goal}",
        confidence=0.75,
        expected_cost=None,
        reason="test fixture",
    )
```

Use this helper for `run_autonomously` tests instead of duplicating eight provenance fields in every lambda.

- [ ] **Step 5: Run Task 3 GREEN**

```bash
uv run ruff check src/computeruse/autonomous.py src/computeruse/scheduler.py tests/smoke/test_autonomous.py tests/smoke/test_scheduler.py
uv run pyright
uv run pytest -q tests/smoke/test_autonomous.py tests/smoke/test_scheduler.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/computeruse/autonomous.py src/computeruse/scheduler.py tests/smoke/test_autonomous.py tests/smoke/test_scheduler.py
git commit -m "feat: rank autonomous memory proposals"
```

---

### Task 4: Carry provenance through inbox and mission scheduling without weakening acquisition safety

**Files:**
- Modify: `src/computeruse/cli.py`
- Modify only if required by failing integration tests: `tests/smoke/test_folder_watch.py`, `tests/smoke/test_missions.py`, `tests/smoke/test_autonomous.py`

**Interfaces:**
- Consumes `UsageStore.records()`, `estimate_expected_cost`, and `make_proposal`.
- Produces inbox proposals with `source_type="operator_inbox"` and `source_id=claimed.task.source_name`.
- Produces mission proposals with `source_type="mission_resume"` and `source_id=mission.mission_id`.
- Calls memory `propose_goal(..., usage=current_usage, ...)` after inbox and mission sources are exhausted.

- [ ] **Step 1: Add RED CLI/source-construction regressions**

Where existing tests already run `_run_autonomous_session`, capture the `GoalProposal` passed into execution and assert:

```python
assert proposal.source_type == "operator_inbox"
assert proposal.source_id == "task.md"
```

For a resumable mission assert:

```python
assert proposal.source_type == "mission_resume"
assert proposal.source_id == mission.mission_id
assert proposal.goal == remaining_goal(mission)
```

Also assert an operator inbox proposal has a higher `utility_score` than a mission proposal created with the same cost, and mission higher than all memory source classes.

If the nested CLI orchestration makes direct capture impractical, test a tiny pure helper introduced in `cli.py` only for constructing these two proposal forms; do not expose session internals merely for tests.

- [ ] **Step 2: Run the focused integration tests RED**

```bash
uv run pytest -q tests/smoke/test_folder_watch.py tests/smoke/test_missions.py tests/smoke/test_autonomous.py
```

Expected: FAIL at proposal construction because current CLI proposals contain only `goal/app/reason`.

- [ ] **Step 3: Wire live usage history into each proposal cycle**

Inside `_run_autonomous_session` create:

```python
usage_store = UsageStore(store / "usage")
```

Inside `propose()`, read `current_usage = usage_store.records()` so a run completed earlier in the same unattended session can influence the next candidate’s expected cost.

Do not snapshot usage only once at session start.

- [ ] **Step 4: Enrich inbox proposals after atomic claim**

Keep `claim_next_task(...)` exactly where it is today. Only after a file is successfully claimed and parsed, create:

```python
make_proposal(
    goal=claimed.task.goal,
    app=claimed.task.app,
    source_type="operator_inbox",
    source_id=claimed.task.source_name,
    confidence=1.0,
    expected_cost=estimate_expected_cost(claimed.task.goal, current_usage),
    reason=(
        f"task file {claimed.task.source_name!r} claimed "
        f"from watched folder {watch_dir}"
    ),
)
```

Never read the unclaimed file body to rank it.

- [ ] **Step 5: Enrich mission proposals without changing resume semantics**

For resumable missions use:

```python
mission_confidence = max(0.5, 1.0 - 0.2 * mission.attempts)
```

Build the proposal from `remaining_goal(mission)` and `mission.mission_id`. The existing attempt ceiling and blocked-mission exclusion remain authoritative.

- [ ] **Step 6: Pass current usage into memory ranking and remove the session RNG**

Delete the now-unused `rng = random.Random()` in `_run_autonomous_session` and call:

```python
return propose_goal(
    skills,
    episodes,
    usage=current_usage,
    exclude=waiting | operator_goals | exhausted_goals,
)
```

- [ ] **Step 7: Run Task 4 GREEN**

```bash
uv run ruff check src/computeruse/cli.py src/computeruse/autonomous.py src/computeruse/scheduler.py tests/smoke/test_autonomous.py tests/smoke/test_scheduler.py tests/smoke/test_folder_watch.py tests/smoke/test_missions.py
uv run pyright
uv run pytest -q \
  tests/smoke/test_scheduler.py \
  tests/smoke/test_autonomous.py \
  tests/smoke/test_folder_watch.py \
  tests/smoke/test_missions.py \
  tests/smoke/test_budget.py \
  tests/smoke/test_killswitch.py
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/computeruse/cli.py src/computeruse/autonomous.py src/computeruse/scheduler.py tests/smoke/test_scheduler.py tests/smoke/test_autonomous.py tests/smoke/test_folder_watch.py tests/smoke/test_missions.py
git commit -m "feat: preserve provenance across autonomous sources"
```

---

### Task 5: Prove legacy behavior and repository-wide quality gates

**Files:**
- Modify only when a regression proves necessary.
- Test all existing Python and Rust suites.

**Interfaces:**
- Proves scheduler changes goal selection only; it does not weaken execution permissions, budgets, idle/reclaim, claim-once, mission continuity, or verification.

- [ ] **Step 1: Run scheduler-specific regressions**

```bash
uv run pytest -q \
  tests/smoke/test_scheduler.py \
  tests/smoke/test_autonomous.py \
  tests/smoke/test_folder_watch.py \
  tests/smoke/test_missions.py \
  tests/smoke/test_sovereign_mode.py \
  tests/smoke/test_trust_mode_safety.py \
  tests/smoke/test_budget.py \
  tests/smoke/test_killswitch.py \
  tests/smoke/test_completion_audit.py
```

Expected: PASS.

- [ ] **Step 2: Run full Python quality gates**

```bash
uv run ruff check .
uv run pyright
uv run pytest -q --tb=short
```

Expected: all GREEN.

- [ ] **Step 3: Run Rust regression gates**

```bash
cargo test --manifest-path driver/Cargo.toml --all-targets
cargo clippy --manifest-path driver/Cargo.toml --all-targets -- -D warnings
```

Expected: GREEN. macOS real-backend gates are additionally proven by repository CI.

- [ ] **Step 4: Inspect the final PR diff for scope creep**

The final changed production files should be limited to scheduler/autonomous/CLI integration. No menu UI, preference model, ActivityEvent stream, skill fast-path, or Sovereign permission changes belong here.

- [ ] **Step 5: Commit any final test-only correction**

```bash
git add src/computeruse/scheduler.py src/computeruse/autonomous.py src/computeruse/cli.py tests/smoke/
git commit -m "test: verify ranked scheduler regression floor"
```

---

## PR Acceptance Criteria

The PR is ready only when all of the following are demonstrated by tests/CI:

1. Every `GoalProposal` has concrete `source_type` and `source_id` provenance.
2. `utility_score`, `confidence`, and `expected_cost` are present and auditable.
3. Identical stored state produces the same ranked proposal without ambient RNG.
4. Explicit operator inbox work outranks resumable missions.
5. Resumable missions outrank all memory-maintenance work.
6. Demoted skill repair outranks failed-episode retry; failed episode outranks unproven skill validation.
7. Exclusion is applied before ranking and never discards valid peers by chance.
8. Historical expected cost is derived only from exact normalized goal matches; unknown remains `None`.
9. Inbox files are still atomically claimed before their content becomes executable work.
10. Mission proposals still use `remaining_goal(mission)` and never restart completed physical sub-goals.
11. Attempt ceilings, blocked approvals, session budgets, idle/reclaim, kill switch, permission guards, completion audit, and verification behavior remain unchanged.
12. Full Python CI and both Rust CI gates are GREEN.

## Explicit Follow-on Work

- PR C: adaptive user-preference model and preference-backed recurring candidates.
- PR D: verified skill fast path and performance metrics.
- PR E+: activity stream, modular menu UI, live timeline, long-run Sovereign benchmark.
