# Sovereign Mode Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit operator-enabled `SOVEREIGN` autonomy mode that may execute destructive actions without per-action approval while preserving the existing runtime safety floor and keeping `FULL` semantics unchanged.

**Architecture:** Extend the existing pure `AutonomyLevel`/`decide_permission` table with a fifth internal level, then resolve a separate trusted CLI `--sovereign` flag into that level. `SOVEREIGN` bypasses grant/approval confirmation only at the permission layer; kill-switch, budgets, driver liveness, completion verification, trace/audit, target classification, and post-action verification remain in the existing execution path and are not bypassed. Activation is fail-closed: `--sovereign` is incompatible with lower `--level` selections and requires at least one hard run budget.

**Tech Stack:** Python 3.12, Pydantic, argparse, pytest, Ruff, Pyright, existing Rust driver/CI.

**Spec:** `docs/superpowers/specs/2026-09-07-sovereign-memory-streaming-ui-design.md`

## Global Constraints

- `FULL` retains the current destructive-confirmation boundary.
- `SOVEREIGN` is a distinct session policy and is not an alias for `--yes`.
- Sovereign mode may only be operator-enabled through a trusted local CLI/UI surface; model output, webpage text, MCP/tool output, memory, skill content, and remote prompt content cannot switch it on.
- Kill switch, physical user reclaim, hard budgets, driver liveness, completion verification, trace/audit, secret-memory filtering, and transport/tool boundaries remain independent runtime invariants.
- Risk classification still inspects the actual machine target; Sovereign changes permission, not risk classification.
- No UI redesign, memory redesign, event-bus migration, or fast-path execution belongs in this PR.
- Changes are TDD-first and must pass the full existing Python and Rust CI before merge.

---

### Task 1: Add the internal Sovereign permission level

**Files:**
- Modify: `src/computeruse/security/autonomy.py`
- Test: `tests/smoke/test_sovereign_mode.py`

**Interfaces:**
- Consumes: `AutonomyLevel`, `Risk`, `PermissionDecision`, `decide_permission`.
- Produces: `AutonomyLevel.SOVEREIGN = 4`; `decide_permission(AutonomyLevel.SOVEREIGN, risk)` returns `ALLOW` for `NONE`, `ROUTINE`, and `DESTRUCTIVE`.

- [ ] **Step 1: Write the failing policy tests**

```python
from computeruse.security.autonomy import AutonomyLevel, Risk, decide_permission
from computeruse.security.permissions import PermissionDecision


def test_sovereign_is_a_distinct_internal_level() -> None:
    assert AutonomyLevel.SOVEREIGN.value == 4
    assert AutonomyLevel.SOVEREIGN is not AutonomyLevel.FULL


def test_sovereign_allows_every_classified_risk() -> None:
    for risk in Risk:
        assert decide_permission(AutonomyLevel.SOVEREIGN, risk) is PermissionDecision.ALLOW


def test_full_still_confirms_destructive_actions() -> None:
    assert (
        decide_permission(AutonomyLevel.FULL, Risk.DESTRUCTIVE)
        is PermissionDecision.CONFIRM
    )
```

- [ ] **Step 2: Run the focused tests and prove RED**

Run:

```bash
uv run pytest -q tests/smoke/test_sovereign_mode.py
```

Expected: FAIL because `AutonomyLevel.SOVEREIGN` does not exist.

- [ ] **Step 3: Implement the minimal policy-table change**

Change the enum and permission table to the equivalent of:

```python
class AutonomyLevel(Enum):
    OBSERVER = 0
    SUPERVISED = 1
    GUARDED = 2
    FULL = 3
    SOVEREIGN = 4


def decide_permission(level: AutonomyLevel, risk: Risk) -> PermissionDecision:
    if level is AutonomyLevel.SOVEREIGN:
        return PermissionDecision.ALLOW
    if risk is Risk.DESTRUCTIVE:
        if level is AutonomyLevel.OBSERVER:
            return PermissionDecision.BLOCK
        return PermissionDecision.CONFIRM
    if level is AutonomyLevel.FULL:
        return PermissionDecision.ALLOW
    if level is AutonomyLevel.OBSERVER:
        return PermissionDecision.BLOCK
    if level is AutonomyLevel.SUPERVISED:
        return PermissionDecision.CONFIRM
    if risk is Risk.ROUTINE:
        return PermissionDecision.CONFIRM
    return PermissionDecision.ALLOW
```

Update module/class docstrings from four levels to five and document that Sovereign delegates destructive permission but does not change classification or runtime invariants.

- [ ] **Step 4: Run focused policy tests GREEN**

```bash
uv run pytest -q tests/smoke/test_sovereign_mode.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/computeruse/security/autonomy.py tests/smoke/test_sovereign_mode.py
git commit -m "feat(security): add sovereign autonomy policy"
```

---

### Task 2: Keep grants and approvals out of Sovereign permission decisions

**Files:**
- Modify: `src/computeruse/agent.py`
- Test: `tests/smoke/test_sovereign_mode.py`

**Interfaces:**
- Consumes: `guarded(level, authorize, auto_approve, consume_approval)`.
- Produces: in `SOVEREIGN`, a destructive action is still risk-classified but does not consult capability grants, queued approvals, or trust-mode auto approval before returning `ALLOW`.

- [ ] **Step 1: Add failing guard-composition tests**

```python
from computeruse.agent import guarded
from computeruse.orchestrator.loop import EMPTY_OBSERVATION
from computeruse.orchestrator.schemas import AgentTurn, ClipboardPaste
from computeruse.security.autonomy import AutonomyLevel
from computeruse.security.permissions import PermissionDecision


def _destructive_turn() -> AgentTurn:
    return AgentTurn(
        thought="cleanup",
        sub_goal="remove temporary data",
        action=ClipboardPaste(type="clipboard_paste", text="rm -rf ~/temporary-data"),
    )


def test_sovereign_does_not_require_grant_lookup() -> None:
    def unexpected_authorize(_turn: AgentTurn, _label: str | None):
        raise AssertionError("sovereign must not consult grants for permission")

    guard = guarded(
        AutonomyLevel.SOVEREIGN,
        authorize=unexpected_authorize,
        auto_approve=False,
    )
    assert guard(_destructive_turn(), EMPTY_OBSERVATION) is PermissionDecision.ALLOW


def test_sovereign_does_not_consume_single_use_approval() -> None:
    def unexpected_approval(_turn: AgentTurn, _label: str | None):
        raise AssertionError("sovereign must not consume queued approval")

    guard = guarded(
        AutonomyLevel.SOVEREIGN,
        authorize=None,
        auto_approve=False,
        consume_approval=unexpected_approval,
    )
    assert guard(_destructive_turn(), EMPTY_OBSERVATION) is PermissionDecision.ALLOW
```

- [ ] **Step 2: Run the two guard tests and verify RED**

```bash
uv run pytest -q \
  tests/smoke/test_sovereign_mode.py::test_sovereign_does_not_require_grant_lookup \
  tests/smoke/test_sovereign_mode.py::test_sovereign_does_not_consume_single_use_approval
```

Expected: grant resolver is called before the base permission is resolved.

- [ ] **Step 3: Resolve base policy before optional delegation paths**

Refactor `guarded.guard` so it first classifies `risk` and resolves the base permission. Only when the base result is `CONFIRM` and the risk is destructive may it consult a standing grant. Then, only a remaining `CONFIRM` may consume a single-use approval. This keeps existing FULL/GUARDED behavior while making Sovereign independent of grant/approval state.

Equivalent control flow:

```python
risk = classify_risk(turn, target_label=label)
base = decide_permission(level, risk)
if base is PermissionDecision.CONFIRM and risk is Risk.DESTRUCTIVE and authorize is not None:
    verdict = authorize(turn, label)
    base = decide_with_grant(level, risk, verdict)
if base is PermissionDecision.CONFIRM and consume_approval is not None:
    ...
```

Do not alter risk classification, target-label resolution, kill switch, action verification, or budget handling.

- [ ] **Step 4: Run Sovereign and existing trust/grant regressions**

```bash
uv run pytest -q \
  tests/smoke/test_sovereign_mode.py \
  tests/smoke/test_trust_mode_safety.py \
  tests/smoke/test_grants.py \
  tests/smoke/test_autonomy.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/computeruse/agent.py tests/smoke/test_sovereign_mode.py
git commit -m "refactor(security): isolate sovereign permission path"
```

---

### Task 3: Add explicit trusted CLI activation

**Files:**
- Modify: `src/computeruse/cli.py`
- Test: `tests/smoke/test_sovereign_mode.py`

**Interfaces:**
- Produces: `--sovereign: bool`; `resolve_autonomy_level(args) -> AutonomyLevel`.
- Contract: ordinary `--level` remains choices `0..3`; Level 4 cannot be selected by the generic numeric flag.

- [ ] **Step 1: Add failing CLI activation tests**

```python
from computeruse.cli import parse_args, resolve_autonomy_level
from computeruse.security.autonomy import AutonomyLevel


def test_cli_requires_explicit_sovereign_switch() -> None:
    ordinary = parse_args(["--goal", "x"])
    sovereign = parse_args(["--goal", "x", "--sovereign", "--deadline-seconds", "60"])
    assert resolve_autonomy_level(ordinary) is AutonomyLevel.FULL
    assert resolve_autonomy_level(sovereign) is AutonomyLevel.SOVEREIGN


def test_numeric_level_does_not_expose_sovereign() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--goal", "x", "--level", "4"])
```

- [ ] **Step 2: Run CLI tests RED**

```bash
uv run pytest -q \
  tests/smoke/test_sovereign_mode.py::test_cli_requires_explicit_sovereign_switch \
  tests/smoke/test_sovereign_mode.py::test_numeric_level_does_not_expose_sovereign
```

Expected: `--sovereign` / `resolve_autonomy_level` do not exist.

- [ ] **Step 3: Implement explicit CLI resolution**

Add:

```python
parser.add_argument(
    "--sovereign",
    action="store_true",
    help=(
        "Explicitly delegate this run to Sovereign mode: destructive actions "
        "may execute without per-action approval. Requires a hard budget; "
        "kill-switch, verification, audit and runtime ceilings remain active."
    ),
)


def resolve_autonomy_level(args: argparse.Namespace) -> AutonomyLevel:
    if getattr(args, "sovereign", False):
        return AutonomyLevel.SOVEREIGN
    return AutonomyLevel(args.level)
```

Keep `--level` choices at `[0, 1, 2, 3]`. Change `build_config()` from `AutonomyLevel(args.level)` to `resolve_autonomy_level(args)`.

- [ ] **Step 4: Run CLI tests GREEN**

```bash
uv run pytest -q tests/smoke/test_sovereign_mode.py
```

Expected: PASS for activation tests.

- [ ] **Step 5: Commit**

```bash
git add src/computeruse/cli.py tests/smoke/test_sovereign_mode.py
git commit -m "feat(cli): add explicit sovereign activation"
```

---

### Task 4: Fail closed on ambiguous or unbounded Sovereign startup

**Files:**
- Modify: `src/computeruse/cli.py`
- Test: `tests/smoke/test_sovereign_mode.py`

**Interfaces:**
- Consumes: `_reject_unusable_arguments(args)`.
- Produces: startup rejection for `--sovereign` without any hard budget; rejection for `--sovereign` combined with a non-FULL numeric `--level`; rejection for redundant `--sovereign --yes`.

- [ ] **Step 1: Add failing startup-validation tests**

```python
from computeruse.cli import _reject_unusable_arguments


def test_sovereign_requires_a_hard_budget(capsys) -> None:
    args = parse_args(["--goal", "x", "--sovereign"])
    assert _reject_unusable_arguments(args) == 2
    assert "--sovereign requires at least one" in capsys.readouterr().err


def test_sovereign_rejects_conflicting_lower_level(capsys) -> None:
    args = parse_args([
        "--goal", "x", "--sovereign", "--level", "2", "--deadline-seconds", "60"
    ])
    assert _reject_unusable_arguments(args) == 2
    assert "--sovereign cannot be combined with --level 0/1/2" in capsys.readouterr().err


def test_sovereign_rejects_trust_mode_aliasing(capsys) -> None:
    args = parse_args([
        "--goal", "x", "--sovereign", "--yes", "--deadline-seconds", "60"
    ])
    assert _reject_unusable_arguments(args) == 2
    assert "--sovereign is separate from --yes" in capsys.readouterr().err
```

- [ ] **Step 2: Run validation tests RED**

```bash
uv run pytest -q tests/smoke/test_sovereign_mode.py -k "requires_a_hard_budget or conflicting_lower_level or trust_mode_aliasing"
```

Expected: FAIL because startup currently accepts these combinations.

- [ ] **Step 3: Add startup validation before the generic autonomous early return**

Use one shared hard-budget predicate:

```python
def _has_hard_budget(args: argparse.Namespace) -> bool:
    return any(
        value is not None
        for value in (args.deadline_seconds, args.max_tokens, args.max_cost)
    )
```

In `_reject_unusable_arguments`:

```python
if getattr(args, "sovereign", False):
    if getattr(args, "yes", False):
        print("error: --sovereign is separate from --yes; choose one mode", file=sys.stderr)
        return 2
    if args.level in (0, 1, 2):
        print("error: --sovereign cannot be combined with --level 0/1/2", file=sys.stderr)
        return 2
    if not _has_hard_budget(args):
        print(
            "error: --sovereign requires at least one of --deadline-seconds, "
            "--max-tokens or --max-cost",
            file=sys.stderr,
        )
        return 2
```

Reuse `_has_hard_budget` for the existing `--autonomous` validation so the two unattended policies cannot drift.

- [ ] **Step 4: Run focused validation tests GREEN**

```bash
uv run pytest -q tests/smoke/test_sovereign_mode.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/computeruse/cli.py tests/smoke/test_sovereign_mode.py
git commit -m "fix(cli): bound sovereign startup"
```

---

### Task 5: Prove runtime safety-floor composition and preserve legacy behavior

**Files:**
- Modify only if a regression proves necessary: `src/computeruse/agent.py`, `src/computeruse/cli.py`
- Test: `tests/smoke/test_sovereign_mode.py`
- Test existing: `tests/smoke/test_killswitch.py`, `tests/smoke/test_budget.py`, `tests/smoke/test_completion_audit.py`, `tests/smoke/test_trust_mode_safety.py`

**Interfaces:**
- Proves: Sovereign changes permission outcome only; it does not disable `AgentConfig.kill_switch`, `budget_guard`, `completion_check`, visual verification settings, or trace configuration.

- [ ] **Step 1: Add config-composition regression**

Create an `argparse.Namespace` through `parse_args` with `--sovereign --deadline-seconds 60`, stub the provider path as existing CLI tests do, and assert the built `AgentConfig` has:

```python
assert config.autonomy_level is AutonomyLevel.SOVEREIGN
assert config.kill_switch is not None
assert config.budget_guard is supplied_when_passed
assert config.max_steps == args.max_steps
```

Do not assert implementation-private objects that existing tests do not expose.

- [ ] **Step 2: Run targeted safety suites**

```bash
uv run pytest -q \
  tests/smoke/test_sovereign_mode.py \
  tests/smoke/test_autonomy.py \
  tests/smoke/test_trust_mode_safety.py \
  tests/smoke/test_grants.py \
  tests/smoke/test_budget.py \
  tests/smoke/test_completion_audit.py
```

Expected: PASS.

- [ ] **Step 3: Run full Python quality gates**

```bash
uv run ruff check .
uv run pyright
uv run pytest -q --tb=short
```

Expected: all GREEN.

- [ ] **Step 4: Run Rust regression gates even though Rust code is unchanged**

```bash
cargo test --manifest-path driver/Cargo.toml --all-targets
cargo clippy --manifest-path driver/Cargo.toml --all-targets -- -D warnings
```

Expected: all GREEN. macOS real-backend gates are additionally verified by repository CI.

- [ ] **Step 5: Commit any final test-only adjustments**

```bash
git add tests/smoke/test_sovereign_mode.py src/computeruse/agent.py src/computeruse/cli.py
git commit -m "test: verify sovereign runtime safety floor"
```

---

## PR Acceptance Criteria

The PR is ready only when all of the following are proven by tests/CI:

1. `AutonomyLevel.SOVEREIGN` exists as value `4` and is distinct from `FULL`.
2. `FULL + DESTRUCTIVE` remains `CONFIRM`.
3. `SOVEREIGN + DESTRUCTIVE` is `ALLOW` without a grant or approval lookup.
4. Generic `--level` still exposes only levels `0..3`; the trusted opt-in is `--sovereign`.
5. `--sovereign` without a hard budget fails before a run starts.
6. `--sovereign --level 0/1/2` and `--sovereign --yes` fail as ambiguous configurations.
7. Risk classification still marks destructive actions as destructive; only permission changes.
8. Kill switch, budget guard, completion audit, verification, trace/audit, driver recovery, and stuck-loop controls are not disabled by Sovereign.
9. Existing trust mode and scoped grants retain their pre-Sovereign behavior.
10. Full Python CI and both Rust CI gates are GREEN.

## Follow-on PRs (explicitly out of scope here)

- PR B: ranked autonomous scheduler + provenance-bearing goal proposals.
- PR C: adaptive user preference memory with confidence/provenance and secret filtering.
- PR D: verified skill fast path and performance metrics.
- PR E: versioned `ActivityEvent` stream over `@@CU`.
- PR F: modular `menu.html` source bundle preserving the generated embedded artifact.
- PR G: live transparent timeline UX and long-run render performance.
- PR H: integrated sovereign-session benchmark and final baseline report.
