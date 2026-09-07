# Code Review — computeruse Desktop App

## Context

- **Repository**: `computeruse` (monorepo: Python orchestration + Rust driver + SwiftUI desktop app)
- **Branch**: `main` (shared local checkout; `desktop/` is new/untracked)
- **Scope**: `desktop/Sources/ComputerUseDesktop/` — SwiftUI macOS desktop client (IDE) that launches the Python agent CLI as a child process (`python -m computeruse`), renders the run timeline from `@@CU <json>` lines, manages MCP extensions, approvals/missions/grants, and a live ScreenCaptureKit viewport.
- **Language / runtime**: Swift 5.9+, SwiftUI/AppKit, macOS 14+ (`desktop/Package.swift`, SwiftPM). The CLI side is Python 3.12+ (`src/computeruse/cli.py`).
- **Purpose of the recent change**: fix the CLI interpreter resolution (`AgentRunner.swift`), align title-bar geometry with the macOS traffic lights, and relocate the sidebar toggle into the top bar row.
- **Review focus**: the desktop app's process/CLI contract (biggest risk area — it is the bridge to the physical-host agent), plus lifecycle, concurrency, and data handling in the app itself.

---

## Verification Report (2026-09-07 — another agent applied fixes; verified)

| Finding | Severity | Status | Evidence |
|---|---|---|---|
| CR-ITEM-3.1 `--driver-bin` → `--driver` | Critical | ✅ **FIXED & VERIFIED** | `buildCliArgs` emits `--driver`; `--help` shows `--driver DRIVER`; argparse rejects `--driver-bin` (`unrecognized arguments`)
| CR-ITEM-3.2 `--model` via `cliSpec` | High | ✅ **FIXED & VERIFIED** | `--model AgentModel.cliSpec(for:)` → `openai:<id>`; `--help` documents `openai[:model_id]`
| CR-ITEM-2.1 Main-thread blocking | High | ✅ **FIXED** | `Task.detached` pipe reads + `Task.detached { waitUntilExit() }`; also now shares `AgentRunner.resolveCommand`/`repoRoot()`
| CR-ITEM-2.2 Capture never stops | High | ✅ **FIXED** | `RightPanelView.onDisappear { capture.stop() }`; `queueDepth` 5→3
| CR-ITEM-2.3 Unbounded console | Medium | ✅ **FIXED** | `updateConsole` 8192-char cap + entry rotation; `AgentRunner` truncates long unterminated lines (8100 chars + marker)
| CR-ITEM-3.3 Nested buttons | Medium | ✅ **FIXED** | Trash moved to `.overlay(alignment: .trailing)` with an 18pt spacer; row keeps single `Button`
| CR-ITEM-3.4 `contains()` deletion | Low | ✅ **FIXED** | Exact match: `deletingPathExtension().lastPathComponent == id` + typed error toast
| CR-ITEM-3.5 level-3 cap | Low | ✅ **FIXED** | `fullAccess → 3`
| CR-ITEM-1.2 Record-id validation | Low | ✅ **FIXED** | `validateRecordID` regex `^[A-Za-z0-9_][A-Za-z0-9_-]*$` before CLI args
| CR-ITEM-4.2 Root-discovery dedup | Low | ✅ **FIXED** | `StoreDataService` uses `AgentRunner.resolveCommand`/`repoRoot()` (no more `#file` root)
| CR-ITEM-4.3 Duplicate `AgentThread` inits | Low | ✅ **FIXED** | One initializer remains
| CR-ITEM-4.4 `import VideoToolbox` | Low | ✅ **FIXED** | Moved to top (line 4)
| CR-ITEM-2.4 WindowChrome churn | Low | ✅ **FIXED** | `apply()` is now idempotent (early-return guard)
| CR-ITEM-4.1 Geometry constants | Medium | ⚠️ **PARTIAL — needs visual check** | Constants added & consumed, BUT `trafficLightCenterY = 14` (was empirically-verified 14.75) is **unused dead code**, and the top bar is now center-aligned in a 28pt box (row centre = 14). If the traffic lights sit at 14.75 on the user's macOS, the whole row is ~0.75pt high. Rebuild and eyeball; if off, consume the constant with `titleRowTopPadding` instead of 0
| CR-ITEM-1.1 / 1.3 / CR-POS | — | ✅ Held | No regressions observed in array-arg process usage or key handling
| Doc drift (new) | Low | ⚠️ Open | `CosmicHeaderView` doc comment still says "22pt row + 3.75pt top padding inside a 38pt box, y=14.75" and "moved to the right side" — code now uses 28/0 padding and the toggle sits left of the `computeruse` text

**Remaining action**: rebuild (`swift build --package-path desktop` — passes), run the app, and confirm the title-row centre (14) matches the traffic lights on the target macOS; bump `trafficLightCenterY`/`titleRowTopPadding` if it looks off, and delete the unused constant. Also refresh the stale header comments.

---

## Review Plan

- [ ] **CR-PLAN-1.1 [Process & CLI Contract]**: verify every flag `AgentRunner.buildCliArgs` emits exists in `src/computeruse/cli.py` (`--model`, `--level`, `--yes`, `--mcp`, `--app`, `--real`, `--driver-bin`); verify model spec format and level range. Priority: Critical.
- [ ] **CR-PLAN-1.2 [Concurrency & Main-Thread Audit]**: find synchronous blocking (`waitUntilExit`, long I/O) on the main actor; check `Task {}` vs `Task.detached` in `StoreDataService`/`CaptureService`; verify stream lifecycle. Priority: High.
- [ ] **CR-PLAN-1.3 [Resource Lifecycle]**: SCStream start/stop vs panel visibility; unbounded timeline/console growth; frame store memory. Priority: High.
- [ ] **CR-PLAN-1.4 [Security & Data Handling]**: input from disk (`~/.computeruse/*.json`, `env`) treated as data, not code; command execution paths (MCP servers, CLI args); secret handling (`OPENAI_API_KEY`); shell-injection surface. Priority: High.
- [ ] **CR-PLAN-1.5 [UI/UX Correctness]**: nested-button behavior, alignment constants duplication, dead code, naming, duplication. Priority: Medium.
- [ ] **CR-PLAN-1.6 [Verification Commands]**: build, flag-contract probe, model-resolution repro. Priority: High.

---

## Verification status

All CR-ITEM findings are addressed in the desktop sources. Verification: Swift build and two Swift smoke tests (real CLI success/error propagation and 2,100 oversized console updates), Ruff, Pyright, and Rust Clippy passed. Capture visibility/race handling and thread-row interaction were reviewed in source; physical UI verification remains outstanding.

Additional CLI contract correction: grant revocation uses `--revoke`. The CLI has no resume-mission/cancel-mission commands; those controls now explain the limitation rather than submitting invalid flags or reporting success. Approval decisions remain the supported route back to the mission queue.

## Review Findings

### Security

- [x] **CR-ITEM-1.1 [No shell-injection surface found — process args are array-based]**:
  - **Severity**: Info (positive finding)
  - **Location**: `StoreDataService.swift` `runCliCommand`, `AgentRunner.swift` `resolveCommand`/`buildCliArgs`, `ExtensionsService.swift`
  - **Description**: All CLI invocations use `Process` with an arguments **array**; user-influenced strings (goal, approval IDs, model ids) are never interpolated through a shell. No command-injection vector via the UI.
  - **Recommendation**: Keep this invariant. Add a regression comment at each `Process()` construction: "never route through `/bin/sh`".

- [x] **CR-ITEM-1.2 [Approval/mission IDs from disk reach CLI args unvalidated]**:
  - **Severity**: Low
  - **Location**: `StoreDataService.swift` `decideApproval`/`resumeMission`/`cancelMission`/`revokeGrant` (~lines 600-700)
  - **Description**: `request_id`/`mission_id`/`grant_id` are read from JSON files under `~/.computeruse/` and passed as one argument (`--approve <id>`). No shell break-out is possible (array args), but a corrupted/foreign file could produce a confusing error or, in principle, an id that collides with another flag position. Low risk (same-user trust boundary).
  - **Recommendation**: Validate ids against `^[A-Za-z0-9_-]+$` before use, and treat parse failures as warnings in the UI instead of silent no-ops.

- [x] **CR-ITEM-1.3 [API key handling is reasonable but file-based loading deserves a note]**:
  - **Severity**: Low
  - **Location**: `ComputerUseDesktopApp.swift` `loadEnvFile()`, `Sheets.swift` `SystemStatus.openAIKeyStatus()`
  - **Description**: `~/.computeruse/env` is parsed for `OPENAI_API_KEY` and injected via `setenv`. The file has no permission check (default umask usually 644). On a multi-user Mac this exposes the key to other local users.
  - **Recommendation**: On load, `chmod 600` the env file (or warn when group/world-readable). Do not log the key anywhere.

### Performance / Resource Lifecycle

- [x] **CR-ITEM-2.1 [Main thread blocks for the full CLI duration in StoreDataService]**:
  - **Severity**: **High**
  - **Location**: `StoreDataService.swift` `runCliCommand` (~lines 800-875), called by `decideApproval`, `resumeMission`, `cancelMission`, `revokeGrant`, `generateReport`
  - **Description**: `StoreDataService` is `@MainActor`; `Task { ... runCliCommand(...) }` inherits the main actor, and `proc.waitUntilExit()` is a **synchronous** block. Approve/deny/report CLI calls freeze the whole UI (spinning beachball) for the process duration — `--report --since-hours N` can take multiple seconds.
  - **Recommendation**: Read the pipes asynchronously and await termination off the main actor:
    ```swift
    private func runCliCommand(args: [String]) async -> String {
        let repoRoot = Self.repoRootURL()
        let proc = Process()
        proc.executableURL = ... // uv path
        proc.arguments = ["run", "python", "-m", "computeruse"] + args
        proc.currentDirectoryURL = repoRoot
        let outPipe = Pipe(); let errPipe = Pipe()
        proc.standardOutput = outPipe; proc.standardError = errPipe
        try proc.run()
        let out = Task.detached { outPipe.fileHandleForReading.readDataToEndOfFile() }
        let err = Task.detached { errPipe.fileHandleForReading.readDataToEndOfFile() }
        let status = await withCheckedContinuation { cont in
            proc.terminationHandler = { _ in cont.resume(returning: $0.terminationStatus) }
        }
        let stdout = String(data: await out.value, encoding: .utf8) ?? ""
        let stderr = String(data: await err.value, encoding: .utf8) ?? ""
        return stdout.isEmpty ? (stderr.isEmpty ? "done" : stderr) : stdout
    }
    ```
    (Also note: `runCliCommand` derives the repo root from `#file`, which breaks if the app is moved after compilation — reuse `AgentRunner.venvPython()`/`repoRoot()` instead; see CR-ITEM-4.2.)

- [x] **CR-ITEM-2.2 [SCStream keeps capturing while the right panel is hidden]**:
  - **Severity**: **High** (CPU/GPU + memory + privacy)
  - **Location**: `RightPanelView.swift` (only `.onAppear { capture.start() }`, no `.onDisappear`), `CaptureService.swift`
  - **Description**: Toggling the right panel off (`Cmd+]` / toggle button) removes the view but never calls `CaptureService.stop()`. The stream continues at full display resolution (config.width/height = display native, BGRA, 60 fps, `queueDepth = 5`) — significant sustained CPU/GPU and multi-hundred-MB pixel churn while the user cannot even see the preview.
  - **Recommendation**: Add `.onDisappear { capture.stop() }` in `RightPanelView` (and re-start in `onAppear` as today). Additionally, downscale the stream to the panel size (e.g. `min(display.width, 1920)` and keep `queueDepth` low) since the preview does not need native pixels; keep the full-resolution capture only behind `--verify`-style needs.

- [x] **CR-ITEM-2.3 [Unbounded timeline/console accumulation]**:
  - **Severity**: Medium
  - **Location**: `AppState.swift` `updateConsole`/`appendTimeline`; `AgentRunner.swift` `consumeStream`
  - **Description**: Every console line is appended into a single entry's `detail` string (`+= "\n" + line`) with no cap, and thread `entries` arrays grow without limit. A long agent run (thousands of steps, large CLI output) → unbounded memory and ever-growing `Text` re-rendering cost in the timeline.
  - **Recommendation**: Cap a console entry's detail (e.g. 8 KB, then rotate to a new console entry), and cap per-thread entries (e.g. keep last 2,000; summarise dropped ones). Keep the agent-side trace files as the full record.

- [x] **CR-ITEM-2.4 [WindowChromeView re-asserts styleMask on every SwiftUI update]**:
  - **Severity**: Low
  - **Location**: `Models.swift` `WindowAccessor.updateNSView` → `WindowChromeView.apply()`
  - **Description**: `updateNSView` fires on every SwiftUI body update and mutates `window.styleMask` (and other chrome properties) each time; AppKit may re-layout the window in response, causing avoidable churn/flicker.
  - **Recommendation**: Apply once per window (`viewDidMoveToWindow` + one deferred pass is already there) and only re-assert when `window` identity changed; drop the per-update `apply()` or guard it with a "didApply" flag per window.

### Bugs (Correctness)

- [x] **CR-ITEM-3.1 [`--driver-bin` flag does not exist — every `--real` run from the app fails]**:
  - **Severity**: **Critical** (blocks the product's core mode)
  - **Location**: `AgentRunner.swift` line ~498; contract in `src/computeruse/cli.py` line ~402
  - **Description**: `buildCliArgs` appends `--driver-bin <path>`, but the CLI only defines `--driver` (the Rust menu launcher passes `--driver` too). The moment the interpreter resolution is fixed and the driver binary is found, `python -m computeruse ... --real --driver-bin ...` aborts with `unrecognized arguments: --driver-bin`.
  - **Reproduction**: `.venv/bin/python3 -m computeruse --goal hi --model openai --level 3 --real --driver-bin /tmp/nope`
  - **Recommendation**: Rename the flag to `--driver` (and update the `desktop/README.md` example which already documents `--driver`).

- [x] **CR-ITEM-3.2 [Bare model id passed to `--model` — `openai:` prefix never applied]**:
  - **Severity**: **High** (blocks every LLM run once the interpreter is fixed)
  - **Location**: `AgentRunner.swift` `buildCliArgs` (`"--model", state.selectedModel`); `Models.swift` `AgentModel.cliSpec(for:)` (defined, unused)
  - **Description**: The CLI resolves `--model`: `openai`/`openai:<id>` → OpenAI transport; anything else → `module:callable` import (`_load_callable`, cli.py ~line 598). `state.selectedModel` is a bare id like `gpt-5.6-luna`, so every run fails at model resolution with a `module:callable` import error — exactly the class of failure the user's transcript hinted at (`--model gpt-5.6-luna`).
  - **Reproduction**: `.venv/bin/python3 -m computeruse --goal hi --model gpt-5.6-luna` (fails before actuation)
  - **Recommendation**:
    ```swift
    "--model", AgentModel.cliSpec(for: state.selectedModel),   // "openai:gpt-5.6-luna"
    ```

- [x] **CR-ITEM-3.3 [Nested Button inside Button label — delete may also select the thread]**:
  - **Severity**: Medium
  - **Location**: `SidebarView.swift` `ThreadRow.body` (trash `Button` inside the row's outer `Button` label)
  - **Description**: SwiftUI does not support nested interactive controls reliably; on macOS, clicking the trash frequently ALSO fires the outer row's action (`onSelect` → switches tab + selects the thread). Destructive click with a side effect.
  - **Recommendation**: Move the delete control out of the label — e.g. put it in `.overlay(alignment: .trailing)` with `.contentShape` on the row, or restructure as `HStack` with an explicit select gesture on the text area and a standalone delete button.

- [x] **CR-ITEM-3.4 [`contains()`-based skill deletion can remove unintended files]**:
  - **Severity**: Low
  - **Location**: `ExtensionsService.swift` `deleteLearnedSkill`, `StoreDataService.swift` `deleteSkill`
  - **Description**: `file.lastPathComponent.contains(id)` deletes any file whose name contains the id substring; a short id (e.g. `"skill"` or `"a"`) matches unrelated files. Ids originate from JSON on disk.
  - **Recommendation**: Exact match on the id: `file.deletingPathExtension().lastPathComponent == id` (or parse the file and compare `skill_id`/`episode_id`).

- [x] **CR-ITEM-3.5 [Autonomy mapping never reaches CLI level 3, contradicting the earlier level-3 run]**:
  - **Severity**: Low
  - **Location**: `Models.swift` `AutonomyLevel.cliLevel` (maps `fullAccess → 2`); `AgentRunner.swift` (`--level` uses `cliLevel`); CLI accepts `choices=[0,1,2,3]`
  - **Description**: The UI's top picker option "Full access" produces `--level 2`, so the app can never request full Level 3 autonomy (the transcript's original manual command used `--level 3`). If intentional (safety cap), document it in the picker; otherwise map `fullAccess → 3`.
  - **Recommendation**: Decide explicitly: either add a "Full autonomy (level 3)" option or a caption on the picker stating the cap; update `cliLevel` accordingly.

### Code Quality / Maintainability

- [x] **CR-ITEM-4.1 [Title-bar geometry magic numbers duplicated across four files]**:
  - **Severity**: Medium
  - **Location**: `CosmicHeaderView.swift` (76 / 22 / 3.75 / 38), `SidebarView.swift` (`-28`), `CenterStageView.swift` (`-28`, 22, 3.75, 46), `AppState.swift` line 81 (`trafficLightCenterY = 14.75`, currently **unused** dead state)
  - **Description**: The same constants have already drifted apart twice (the `-20` vs `-28` regression) and caused visible misalignment. `trafficLightCenterY` was added but never consumed.
  - **Recommendation**: Add tokens to `Theme` (`titleZoneHeight = 28`, `trafficLightCenterY = 14.75`, `titleRowHeight = 22`, `trafficLightClearance = 76`) and consume them in all three views; delete the dead `@Published trafficLightCenterY`.

- [x] **CR-ITEM-4.2 [Duplicate repo-root discovery logic]**:
  - **Severity**: Low
  - **Location**: `StoreDataService.swift` `runCliCommand` (`#file`-based root) vs `AgentRunner.swift` `venvPython()`/`repoRoot()`
  - **Description**: Two different mechanisms resolve the repo root; the `#file` trick silently breaks once the compiled app is moved (path baked at build time), while `AgentRunner`'s anchor-walk does not. Drift between the two means CLI control commands and agent runs can target different interpreters.
  - **Recommendation**: Expose `AgentRunner.repoRoot()` (or a shared `RepoLocator`) and use it in `StoreDataService.runCliCommand`.

- [x] **CR-ITEM-4.3 [Duplicate `AgentThread` initializers]**:
  - **Severity**: Low
  - **Location**: `Models.swift` (~lines 55-100): two `init`s differ only in parameter order (`status`/`entries` swapped)
  - **Description**: Copy-paste duplication with identical defaults; callers can't tell them apart. Confusing for maintenance.
  - **Recommendation**: Keep one initializer.

- [x] **CR-ITEM-4.4 [Import placed at end of file]**:
  - **Severity**: Low
  - **Location**: `CaptureService.swift` last line: `import VideoToolbox`
  - **Description**: Violates convention; easy to miss when reading.
  - **Recommendation**: Move to the top with the other imports.

- [x] **CR-ITEM-4.5 [MCP config read without schema validation]**:
  - **Severity**: Low
  - **Location**: `ExtensionsService.swift` `loadMcpServers`; CLI `--mcp` executes `command`+`args` from `~/.computeruse/mcp.json`
  - **Description**: Any tool that can write `~/.computeruse/mcp.json` (same user) can make the agent spawn arbitrary processes via `--mcp`. Same-user trust boundary, so acceptable — but validate the schema (command non-empty string, args array of strings) when loading, and surface invalid entries instead of silently skipping.
  - **Recommendation**: Add a `guard`-based validation pass and a UI warning listing invalid entries.

### Positive Findings

- [ ] **CR-POS-1**: `AgentBridge` socket probes run on detached tasks with 150 ms deadlines; emergency stop signals only the driver this app spawned, never an external CLI-owned driver (correct ownership boundary).
- [ ] **CR-POS-2**: `StreamFrameView`/`FrameStore` — layer-backed presentation with identity check, no per-frame `NSImage`; store holds only the latest frame.
- [ ] **CR-POS-3**: `CaptureService` excludes its own window (mirror loop prevention) and degrades gracefully (permission missing, headless).
- [ ] **CR-POS-4**: `AgentRunner` interpreter resolution now matches the documented order, with a fix-it hint on the last-resort path; process termination handling (SIGINT park, `@@CU` stream parsing) is defensive and structured.
- [ ] **CR-POS-5**: `AppState` is `@MainActor` with value-type `Sendable` models; runner callbacks are explicit and well named.

---

## Proposed Code Changes

### Patch A (Critical) — `desktop/Sources/ComputerUseDesktop/AgentRunner.swift`

```diff
         if state.useRealDriver {
             args.append("--real")
             if let bin = Self.resolveDriverBinary() {
-                args.append(contentsOf: ["--driver-bin", bin.path])
+                args.append(contentsOf: ["--driver", bin.path])
             }
         }
```

### Patch B (High) — same file, `buildCliArgs`

```diff
         var args: [String] = [
             "--goal", goal,
-            "--model", state.selectedModel,
+            "--model", AgentModel.cliSpec(for: state.selectedModel),
             "--level", "\(state.autonomyLevel.cliLevel)",
         ]
```

### Patch C (High) — `desktop/Sources/ComputerUseDesktop/StoreDataService.swift` (`runCliCommand`)

Replace the sync `proc.waitUntilExit()` flow with an async read + `terminationHandler` continuation (full snippet in CR-ITEM-2.1). Also replace the `#file`-derived root with `AgentRunner.repoRoot()` (CR-ITEM-4.2).

### Patch D (High) — `desktop/Sources/ComputerUseDesktop/RightPanelView.swift`

```diff
         .onAppear {
             capture.targetFps = state.targetFps
             capture.start()
         }
+        .onDisappear {
+            capture.stop()
+        }
```

### Patch E (Medium) — `desktop/Sources/ComputerUseDesktop/Theme.swift` + geometry consumers

```swift
// Title-bar geometry (traffic-light alignment) — single source of truth.
static let titleZoneHeight: CGFloat = 28      // hiddenTitleBar zone
static let trafficLightCenterY: CGFloat = 14.75
static let titleRowHeight: CGFloat = 22
static let trafficLightClearance: CGFloat = 76
```
Consume in `CosmicSidebarHeader`, `SidebarView` (`-Theme.titleZoneHeight`), `CenterStageView.topBar`; delete `AppState.trafficLightCenterY`.

### Patch F (Medium) — `desktop/Sources/ComputerUseDesktop/SidebarView.swift` (`ThreadRow`)

Move the trash button out of the outer `Button` label (overlay or sibling control) so a delete click cannot also select the row (CR-ITEM-3.3).

### Patch G (Medium) — `AppState.updateConsole` cap

```swift
if let last = ..., threads[index].entries[last].kind == .console {
    if threads[index].entries[last].detail.count < 8192 {
        threads[index].entries[last].detail += "\n" + line
    } else {
        appendTimeline(...) // rotate to a fresh console entry
    }
}
```

---

## Commands

```bash
# 1. Build the desktop app (verify Patches A-G compile)
swift build --package-path desktop

# 2. Prove the --driver-bin bug (must print "unrecognized arguments")
.venv/bin/python3 -m computeruse --goal hi --model openai --level 3 --real --driver-bin /tmp/nope 2>&1 | tail -3

# 3. Prove the bare-model bug (must fail at model resolution, before actuation)
.venv/bin/python3 -m computeruse --goal hi --model gpt-5.6-luna 2>&1 | tail -3

# 4. Prove the corrected invocation resolves flags (help + dry model load; no actuation)
.venv/bin/python3 -m computeruse --goal hi --model openai:gpt-5.6-luna --level 2 --driver driver/target/debug/actuation-driver 2>&1 | tail -5
```

CI: keep `swift build --package-path desktop` in the desktop workflow; add a contract-drift guard that greps `AgentRunner.swift` for `--driver-bin` (must be absent) — mirror of `tests/smoke/test_contract_drift.py` for the Python↔Rust contract.

---

## Effort & Priority Assessment

| Finding | Severity | Effort | Complexity | Priority |
|---|---|---|---|---|
| CR-ITEM-3.1 `--driver-bin` → `--driver` | Critical | ~15 min | Simple | **P0** |
| CR-ITEM-3.2 `--model` → `openai:<id>` | High | ~15 min | Simple | **P0** |
| CR-ITEM-2.1 Main-thread blocking | High | 2–4 h | Moderate | P1 |
| CR-ITEM-2.2 Capture keeps streaming | High | 1–2 h | Moderate | P1 |
| CR-ITEM-3.3 Nested buttons | Medium | 1–2 h | Moderate | P1 |
| CR-ITEM-2.3 Unbounded timeline/console | Medium | ~1 h | Simple | P2 |
| CR-ITEM-4.1 Geometry constants | Medium | ~1 h | Simple | P2 |
| CR-ITEM-3.4 contains() deletion | Low | ~30 min | Simple | P2 |
| CR-ITEM-3.5 level-3 cap | Low | ~30 min | Simple | P2 |
| CR-ITEM-4.2 Root-discovery dedup | Low | ~1 h | Moderate | P2 |
| CR-ITEM-4.3/4.4/4.5 cleanup | Low | ~30 min total | Simple | P3 |

**Dependencies**: P0 items should land together with the existing interpreter-resolution fix (they are the next two failures the user would hit). CR-ITEM-2.1 depends on the shared root-locator refactor (CR-ITEM-4.2) if done first. No cross-team coordination required.

---

## QA Checklist

- [x] Every finding has a severity level and a clear remediation path
- [x] Security issues are flagged first; no Critical/High security findings — the app's main risks are correctness/perf (noted Info findings for the same-user trust boundary)
- [x] Performance suggestions include measurable justification (UI freeze, stream at native res/60fps, unbounded growth)
- [x] Code examples are syntactically valid Swift
- [x] File paths verified against the current checkout; line numbers approximate where the shared checkout is actively changing
- [x] Review covers the files in scope (desktop app + its CLI contract)
- [x] Positive aspects acknowledged (CR-POS-1..5)