# Adaptive Preference Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the agent a typed, provenance-bearing user-preference memory that learns only from verified evidence, resists secrets and contradictions, and injects only active preferences into future working context.

**Architecture:** Add a dedicated `memory/preferences.py` functional core plus an imperative `PreferenceStore`, rather than overloading generic semantic memory. Preference evidence is accumulated by stable `(domain,key,value)` identity, contradictions remain in history and may supersede only after stronger evidence, and retrieval exposes only active unsuperseded records. Agent integration learns after verified completion and stages compact safe preference summaries before the first OODA turn.

**Tech Stack:** Python 3.12, Pydantic v2, dataclasses, existing atomic JSON-file storage, pytest, Ruff, Pyright.

**Spec:** `docs/superpowers/specs/2026-09-07-sovereign-memory-streaming-ui-design.md` §6.2–6.3.

## Global Constraints

- Explicit user instructions have the strongest confidence.
- One inferred/repeated behavior never becomes an active durable preference by itself.
- Contradictory evidence never silently destroys the previous record; history remains inspectable through `supersedes`.
- Preference writes are allowed only from verified outcomes. Failure, forced/unverified completion, kill-switch termination, or empty trajectories cannot increase preference confidence.
- Passwords, tokens, API keys, authorization headers, PEM material, JWT-like credentials, and other credential-like strings are rejected before persistence.
- Raw typed/pasted action text is never used as preference evidence. Existing semantic extraction already stores text length rather than contents; this PR keeps that invariant.
- Current semantic memory remains backward compatible. No destructive migration of existing `SemanticEntry(kind="preference")` records.
- Preference retrieval is bounded and deterministic.
- PR E owns the versioned `ActivityEvent` transport. This PR exposes a typed `PreferenceWrite` callback seam but does not invent a second event protocol.
- No new runtime dependency.

---

## File Map

- Modify `src/computeruse/memory/schemas.py`: add `PreferenceRecord`, `PreferenceDomain`, and `PreferenceSource`.
- Create `src/computeruse/memory/preferences.py`: evidence model, secret gate, confidence/conflict rules, active retrieval, explicit durable-instruction extraction, and `PreferenceStore`.
- Modify `src/computeruse/agent.py`: open preference store, stage active preferences, learn only from verified completion, expose callback/result data.
- Modify `AGENTS.md`: Law 4.2 directory map and learning rule for the typed user model.
- Create `tests/smoke/test_preferences.py`: pure/store/conflict/secret/extraction tests.
- Modify `tests/smoke/test_agent_learning.py` or the nearest existing agent-memory integration test: verified-only learning and retrieval.

---

### Task 1: Typed preference record and evidence contract

**Files:**
- Modify: `src/computeruse/memory/schemas.py`
- Create: `src/computeruse/memory/preferences.py`
- Create: `tests/smoke/test_preferences.py`

**Interfaces:**

```python
PreferenceDomain = Literal[
    "ui", "workflow", "communication", "app", "scheduling", "formatting", "general"
]
PreferenceSource = Literal["explicit", "repeated_behavior", "successful_correction"]

class PreferenceRecord(BaseModel):
    preference_id: str
    domain: PreferenceDomain
    key: str
    value: str
    confidence: float
    evidence_count: int
    source: PreferenceSource
    evidence_ids: tuple[str, ...]
    first_seen: datetime
    last_seen: datetime
    supersedes: str | None = None
```

```python
@dataclass(frozen=True)
class PreferenceEvidence:
    domain: PreferenceDomain
    key: str
    value: str
    source: PreferenceSource
    source_id: str
    observed_at: datetime
```

- [ ] **Step 1: Write RED schema/evidence tests**

Tests must prove field retention, confidence bounds `[0,1]`, non-empty key/value/source id, stable preference identity for equal `(domain,key,value)`, and different identity for contradictory values.

- [ ] **Step 2: Run RED**

```bash
uv run pytest -q tests/smoke/test_preferences.py
```

Expected: import/attribute failure because preference types do not exist.

- [ ] **Step 3: Implement minimal types and deterministic identity**

Use SHA-256 over normalized `domain + "\0" + key + "\0" + value`, with a readable slug prefix and 12 hex digest characters. Normalize surrounding/repeated whitespace only; never lowercase the stored value.

- [ ] **Step 4: Run GREEN**

```bash
uv run ruff check src/computeruse/memory/schemas.py src/computeruse/memory/preferences.py tests/smoke/test_preferences.py
uv run pyright
uv run pytest -q tests/smoke/test_preferences.py
```

- [ ] **Step 5: Commit**

```bash
git add src/computeruse/memory/schemas.py src/computeruse/memory/preferences.py tests/smoke/test_preferences.py
git commit -m "feat(memory): add typed preference evidence contract"
```

---

### Task 2: Secret gate and pure confidence/conflict rules

**Files:**
- Modify: `src/computeruse/memory/preferences.py`
- Modify: `tests/smoke/test_preferences.py`

**Interfaces:**

```python
def contains_sensitive_preference_material(text: str) -> bool: ...

def apply_preference_evidence(
    records: tuple[PreferenceRecord, ...],
    evidence: PreferenceEvidence,
) -> PreferenceWrite: ...

@dataclass(frozen=True)
class PreferenceWrite:
    outcome: Literal["created", "reinforced", "pending", "superseded", "rejected_sensitive"]
    record: PreferenceRecord | None
    replaced_id: str | None
    safe_summary: str
```

Rules:

- `explicit`: confidence `1.0`, active immediately.
- `successful_correction`: confidence `0.90`, active immediately.
- `repeated_behavior`: first evidence confidence `0.45`; each consistent additional distinct source id adds `0.20`, capped at `0.85`; it is inactive until `evidence_count >= 2` and confidence `>= 0.60`.
- Duplicate `source_id` is idempotent and must not increase evidence count/confidence.
- Same `(domain,key,value)` reinforces the same record.
- Contradictory explicit/correction evidence creates/updates its own record and sets `supersedes` to the current active record for the same `(domain,key)`.
- Contradictory repeated behavior remains a challenger until it becomes active; once active and its confidence is strictly greater than the incumbent inferred record, it may supersede it. It never automatically supersedes an explicit record.
- Sensitive evidence returns `rejected_sensitive` and persists nothing.

Sensitive patterns must include at minimum: `password/passwd`, `api_key/api-key`, `token`, `secret`, `authorization: bearer`, `sk-...`, `ghp_...`, `glpat-...`, `xox[baprs]-...`, JWT-like three-segment values, `AKIA...`, and PEM private-key headers.

- [ ] **Step 1: Add RED tests for all rules above**
- [ ] **Step 2: Run RED focused tests**

```bash
uv run pytest -q tests/smoke/test_preferences.py -k "secret or reinforce or duplicate or contradiction or supersede or repeated"
```

- [ ] **Step 3: Implement pure rules**
- [ ] **Step 4: Run Task 2 GREEN with Ruff, Pyright and preference tests**
- [ ] **Step 5: Commit**

```bash
git add src/computeruse/memory/preferences.py tests/smoke/test_preferences.py
git commit -m "feat(memory): reconcile preference evidence safely"
```

---

### Task 3: PreferenceStore and deterministic active retrieval

**Files:**
- Modify: `src/computeruse/memory/preferences.py`
- Modify: `tests/smoke/test_preferences.py`

**Interfaces:**

```python
def active_preferences(
    records: tuple[PreferenceRecord, ...],
    *,
    domain: PreferenceDomain | None = None,
    limit: int = 16,
) -> tuple[PreferenceRecord, ...]: ...

class PreferenceStore:
    def __init__(self, store_dir: Path) -> None: ...
    def records(self) -> tuple[PreferenceRecord, ...]: ...
    def record(self, evidence: PreferenceEvidence) -> PreferenceWrite: ...
    def active(self, *, domain: PreferenceDomain | None = None, limit: int = 16) -> tuple[PreferenceRecord, ...]: ...
```

Store one JSON file per `preference_id`. `record()` calls the pure reconciliation function, atomically writes only the returned record when one exists, and leaves superseded records on disk. Corrupt files are skipped with warnings rather than hiding healthy memory.

Active retrieval excludes records referenced by another record's `supersedes`, excludes pending one-off repeated behavior, and sorts by `(domain, key, -confidence, preference_id)` before applying the bound.

- [ ] **Step 1: RED store/restart/history tests**
- [ ] **Step 2: Run RED**
- [ ] **Step 3: Implement store**
- [ ] **Step 4: Run GREEN**
- [ ] **Step 5: Commit**

```bash
git add src/computeruse/memory/preferences.py tests/smoke/test_preferences.py
git commit -m "feat(memory): persist active preference history"
```

---

### Task 4: Conservative automatic evidence extraction

**Files:**
- Modify: `src/computeruse/memory/preferences.py`
- Modify: `tests/smoke/test_preferences.py`

**Interfaces:**

```python
def extract_explicit_preference_evidence(
    goal: str,
    *,
    source_id: str,
    observed_at: datetime,
) -> tuple[PreferenceEvidence, ...]: ...
```

Extraction is deliberately conservative. It recognizes only durable-user-intent clauses, not arbitrary task text:

- English cues: `I prefer ...`, `always ...`, `from now on ...`.
- Turkish cues: `tercihim ...`, `tercih ederim ...`, `her zaman ...`, `bundan sonra ...`.
- A line in explicit `preference: key=value` or `tercih: key=value` form maps the key directly; natural-language clauses use `domain="general"`, a deterministic `instruction.<slug>` key, and the clause as value.
- Maximum stored clause length: 240 characters.
- Sensitive clauses are discarded before evidence construction.
- No `type_text`/clipboard payload is inspected.

This extractor labels evidence `source="explicit"` because the durable cue appears in the user's goal itself. Repeated-behavior and successful-correction evidence use the same store API but are not hallucinated from generic trajectories in this task.

- [ ] **Step 1: RED extraction tests in English/Turkish plus negative examples and secret examples**
- [ ] **Step 2: Run RED**
- [ ] **Step 3: Implement conservative extractor**
- [ ] **Step 4: Run GREEN**
- [ ] **Step 5: Commit**

```bash
git add src/computeruse/memory/preferences.py tests/smoke/test_preferences.py
git commit -m "feat(memory): extract explicit durable preferences"
```

---

### Task 5: Wire verified-only learning and retrieval into Agent

**Files:**
- Modify: `src/computeruse/agent.py`
- Modify/create: nearest agent-memory integration test under `tests/smoke/`

**Interfaces:**

Add to `AgentConfig`:

```python
on_preference_write: Callable[[PreferenceWrite], None] | None = None
```

Add to `AgentResult`:

```python
preferences: tuple[PreferenceRecord, ...] = ()
```

Run behavior:

1. Open `PreferenceStore(store_dir / "preferences")` at run start.
2. Stage active preference summaries before OODA, bounded to 16 records, after app semantic knowledge. Render as `[preference:{domain}] {key}: {value}`; never include evidence ids or secret-like raw data in provider context.
3. In `on_complete`, only when `verified = outcome == "success" and not forced_completion` and `trajectory.steps` is non-empty, extract explicit evidence from the original goal using `run_id` provenance and record it.
4. For every returned `PreferenceWrite`, call `on_preference_write` when configured. This is the PR E event seam; callback failures are logged and must not turn a completed physical task into failure.
5. Failed or forced completion produces no preference reinforcement.
6. Return current active preferences in `AgentResult`.

- [ ] **Step 1: RED integration tests**

Tests must prove:
- verified run with `"From now on always use compact summaries"` writes/stages a preference on the next run,
- failed run writes none,
- forced/unverified completion writes none,
- sensitive durable-looking instruction writes none,
- callback receives safe `PreferenceWrite`,
- callback failure does not change the physical run outcome.

- [ ] **Step 2: Run RED integration tests**
- [ ] **Step 3: Implement Agent wiring**
- [ ] **Step 4: Run focused GREEN**
- [ ] **Step 5: Run full Python regression**

```bash
uv run ruff check .
uv run pyright
uv run pytest -q --tb=short
```

- [ ] **Step 6: Commit**

```bash
git add src/computeruse/agent.py src/computeruse/memory/preferences.py src/computeruse/memory/schemas.py tests/smoke
git commit -m "feat(agent): learn verified user preferences"
```

---

### Task 6: Constitution/docs alignment and final gates

**Files:**
- Modify: `AGENTS.md`

Document that Law 4.2 now has a dedicated typed preference store beside generic semantic app knowledge, that inferred preferences require repeated evidence, contradictions preserve history, and verified-only learning plus secret filtering are runtime invariants.

- [ ] **Step 1: Update only current normative docs; do not rewrite historical specs/plans**
- [ ] **Step 2: Run full CI-equivalent gates**

```bash
uv run ruff check .
uv run pyright
uv run pytest -q --tb=short
cargo test --manifest-path driver/Cargo.toml --all-targets
cargo clippy --manifest-path driver/Cargo.toml --all-targets -- -D warnings
```

- [ ] **Step 3: Adversarial self-review**

Check specifically for preference poisoning, duplicate evidence inflation, secret persistence, contradictory overwrite, learning from failed/forced outcomes, unbounded context injection, and mutation of existing semantic-memory files.

- [ ] **Step 4: Commit, update PR evidence, request review/merge only after fresh GREEN**

## Self-Review

- Spec §6.2 fields: covered by Tasks 1–3.
- Explicit > inferred confidence: Task 2.
- One behavior not durable: Task 2 active threshold.
- Contradiction/supersession without erased history: Tasks 2–3.
- Secret/private-content guard: Tasks 2 and 4; raw action text is excluded by construction.
- `memory_written` requirement: PR C provides typed `PreferenceWrite` callback; versioned streaming publication remains deliberately in PR E, the approved event-system phase.
- Verified-only learning from §6.3: Task 5.
- Placeholder scan: no TBD/TODO or undefined neighboring interfaces.
