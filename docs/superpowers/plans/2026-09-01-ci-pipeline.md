# GitHub Actions CI Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single-file GitHub Actions CI workflow (`.github/workflows/ci.yml`) that runs Python lint+typecheck+test and Rust test+clippy on every push to main and every PR.

**Architecture:** Two independent jobs in one workflow — Python and Rust run in parallel. Both use Ubuntu runners (macOS-gated Rust code is properly `#[cfg]`-gated, so the driver compiles with SimulatedBackend on Linux). Python tests include smoke tests that spawn the driver binary, so the Rust build must complete before Python tests can run.

**Tech Stack:** GitHub Actions, `uv` (Python package manager), `cargo` (Rust), `pytest`, `pyright`, `ruff`, `clippy`

---

## Key Technical Decisions

### Why Ubuntu, not macOS?

The Rust driver's macOS-specific modules (`quartz.rs`, `ax.rs`, `indicator.rs`, `menu.rs`, `hotkey.rs`) are gated with `#[cfg(target_os = "macos")]` in `lib.rs`. On Linux, only the pure modules (`bezier.rs`, `protocol.rs`, `backend.rs`) compile — and that's enough for `cargo test` and `cargo clippy`. The driver binary runs with `SimulatedBackend` (no `--real` flag), which is the default and doesn't need macOS APIs.

The Python smoke tests that spawn the driver binary (`test_driver_rpc.py`, `test_grounding.py`, `test_loop.py`, etc.) work on Linux because the binary compiles and runs in simulated mode.

**Result:** Single Ubuntu runner = fast, cheap, no macOS runner cost.

### Why not a matrix?

No matrix needed. Both Python and Rust use the same OS (Ubuntu). A matrix would add complexity without benefit.

### Driver build dependency

Python smoke tests need the compiled driver binary (`driver/target/debug/actuation-driver`). Two options:

- **Option A (chosen):** Build the driver in a separate Rust job, upload the binary as an artifact, download it in the Python job. Clean separation, reuses the Rust build.
- **Option B:** Build the driver inside the Python job. Simpler but mixes concerns and duplicates the Rust build.

---

## File Structure

| File | Action | Purpose |
|------|--------|---------|
| `.github/workflows/ci.yml` | Create | The CI workflow |

---

## Task 1: Write the CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Create the workflow file**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

# Cancel redundant runs on the same branch/PR (saves runner minutes).
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  # ── Rust: test + clippy ──────────────────────────────────────────
  rust:
    name: Rust (test + clippy)
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: driver
    steps:
      - uses: actions/checkout@v4

      - name: Install Rust toolchain
        uses: dtolnay/rust-toolchain@stable
        with:
          components: clippy

      - name: Cache cargo registry + build
        uses: actions/cache@v4
        with:
          path: |
            ~/.cargo/registry
            ~/.cargo/git
            driver/target
          key: cargo-${{ runner.os }}-${{ hashFiles('driver/Cargo.lock') }}
          restore-keys: cargo-${{ runner.os }}-

      - name: Run tests
        run: cargo test --all-targets

      - name: Clippy (deny warnings)
        run: cargo clippy --all-targets -- -D warnings

      - name: Upload driver binary for Python job
        uses: actions/upload-artifact@v4
        with:
          name: driver-binary
          path: driver/target/debug/actuation-driver
          retention-days: 1

  # ── Python: lint + typecheck + test ──────────────────────────────
  python:
    name: Python (ruff + pyright + pytest)
    runs-on: ubuntu-latest
    needs: rust
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v6

      - name: Cache uv
        uses: actions/cache@v4
        with:
          path: ~/.cache/uv
          key: uv-${{ runner.os }}-${{ hashFiles('uv.lock') }}
          restore-keys: uv-${{ runner.os }}-

      - name: Install dependencies
        run: uv sync --dev

      - name: Download driver binary
        uses: actions/download-artifact@v4
        with:
          name: driver-binary
          path: driver/target/debug

      - name: Make driver binary executable
        run: chmod +x driver/target/debug/actuation-driver

      - name: Ruff lint
        run: uv run ruff check .

      - name: Pyright typecheck
        run: uv run pyright

      - name: Pytest
        run: uv run pytest -q
```

- [ ] **Step 2: Verify workflow YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
Expected: No output (valid YAML)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add GitHub Actions workflow for Python and Rust"
```

---

## Design Rationale

### Concurrency control
`cancel-in-progress: true` cancels the previous run when a new push arrives on the same branch. Saves runner minutes on rapid iteration.

### Rust job first, Python depends on it
Python smoke tests (`test_driver_rpc`, `test_grounding`, `test_loop`, etc.) spawn the driver binary via the `conftest.py` session fixture. The binary must exist at `driver/target/debug/actuation-driver` before pytest runs. Building it in a separate Rust job and uploading as an artifact is cleaner than mixing concerns.

### Caching strategy
- **uv:** `~/.cache/uv` keyed on `uv.lock` hash. uv is fast enough that caching is optional but saves ~5s per run.
- **cargo:** Registry + git index + `target/` directory keyed on `Cargo.lock` hash. The Rust build is the slowest step (~30s); caching cuts it to ~5s on cache hits.

### Why `--all-targets` for clippy?
`--all-targets` includes tests and benches, not just the library. This catches lint issues in test code too.

### Why `uv run` for pytest/ruff/pyright?
The project uses `uv` as its package manager. `uv run` ensures the correct virtualenv is active and dependencies are installed.

### No macOS runner
The Rust driver's macOS modules are `#[cfg(target_os = "macos")]`-gated in `lib.rs`. On Linux, only `bezier.rs`, `protocol.rs`, and `backend.rs` compile — enough for `cargo test` and `clippy`. The driver binary runs in simulated mode (default), which doesn't need macOS APIs.

---

## Expected Test Results

After implementation, the workflow should show:

```
rust:    24 passed, clippy clean
python:  246 passed, ruff clean, pyright 0 errors
```
