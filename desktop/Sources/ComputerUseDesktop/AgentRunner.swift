import Foundation

/// Structured step line prefix printed by CLI to stdout
/// (`orchestrator/trace.py:EVENT_PREFIX`). Lines not starting with this prefix
/// are treated as log output and routed to console.
private let eventPrefix = "@@CU "

/// Real agent execution: launches `python -m computeruse --goal … --model … --level N`
/// (interpreted by the repo's `.venv/bin/python3` when found — see
/// `resolveCommand`) as a `Process`, converts stdout `@@CU <json>` step
/// lines into timeline entries, and streams stderr/log lines to console.
///
/// Stopping = SIGINT (`process.interrupt()`), matching the CLI kill-switch
/// (Law 5: user instantly takes back physical control).
/// When `useRealDriver` is off, CLI runs with simulated driver — host cursor
/// never moves; `--real` is only activated deliberately from Settings.
@MainActor
final class AgentRunner {
    private weak var state: AppState?
    private var process: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?
    private var runThreadID: UUID?
    private var runStartedAt: Date = Date()
    private var stepCount: Int = 0
    private var lastPlanText: String = ""
    private var failureSignature: String = ""
    private var consecutiveFailures: Int = 0
    private var stoppedByUser: Bool = false

    private(set) var isRunning: Bool = false

    func attach(state: AppState) {
        self.state = state
    }

    // MARK: - Lifecycle

    /// Starts a new run. Model and permission level are read from current `AppState`.
    func start(threadID: UUID, goal: String) {
        guard let state else { return }
        do { launch(threadID: threadID, goal: goal, cliArgs: try buildCliArgs(goal: goal, state: state)) }
        catch { state.runnerDidFinish(threadID: threadID, success: false, summary: error.localizedDescription, latency: 0, stopped: false); StoreDataService.shared.showToast(error.localizedDescription) }
    }

    func resume(threadID: UUID, goal: String, planID: String) {
        guard let state else { return }
        do {
            launch(threadID: threadID, goal: goal,
                cliArgs: try buildCliArgs(goal: goal, state: state) + ["--resume", planID, "--plan"])
        } catch { StoreDataService.shared.showToast(error.localizedDescription) }
    }

    func preview(threadID: UUID, goal: String) {
        launch(threadID: threadID, goal: goal, cliArgs: ["--goal", goal, "--plan-only", "--level", "0"])
    }

    func executePreview(threadID: UUID, goal: String) {
        guard let state else { return }
        do { launch(threadID: threadID, goal: goal, cliArgs: try buildCliArgs(goal: goal, state: state) + ["--plan"]) }
        catch { StoreDataService.shared.showToast(error.localizedDescription) }
    }

    private func launch(threadID: UUID, goal: String, cliArgs: [String]) {
        guard !isRunning, let state else { return }
        stop()

        stoppedByUser = false
        failureSignature = ""
        consecutiveFailures = 0
        stepCount = 0
        lastPlanText = ""
        runThreadID = threadID
        runStartedAt = Date()

        let resolved = Self.resolveCommand(cliArgs: cliArgs)
        let (executable, args) = (resolved.executable, resolved.args)

        // Keep arguments separate; never route through /bin/sh.
        let proc = Process()
        proc.executableURL = executable
        proc.arguments = args
        proc.currentDirectoryURL = Self.repoRoot()

        var env = ProcessInfo.processInfo.environment
        if env["PATH"] == nil || env["PATH"]?.isEmpty == true {
            env["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
        } else if let p = env["PATH"], !p.contains("/opt/homebrew/bin") {
            env["PATH"] = "/opt/homebrew/bin:" + p
        }
        env["PYTHONUNBUFFERED"] = "1"
        proc.environment = env

        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        stdoutPipe = out
        stderrPipe = err

        proc.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.handleTermination()
            }
        }

        readPipe(out, isError: false, threadID: threadID)
        readPipe(err, isError: true, threadID: threadID)

        do {
            try proc.run()
        } catch {
            state.threadsStore.appendTimeline(
                threadID: threadID,
                entry: TimelineEntry(
                    kind: .action, title: "failed to launch",
                    detail: "\(executable.path) could not be executed: \(error.localizedDescription)",
                    isFailure: true))
            state.runnerDidFinish(
                threadID: threadID, success: false,
                summary: "CLI failed to launch", latency: 0, stopped: false)
            return
        }
        process = proc
        isRunning = true
        state.runnerDidStart(threadID: threadID)
        state.threadsStore.appendTimeline(
            threadID: threadID,
            entry: TimelineEntry(
                kind: .console, title: "running",
                detail:
                    "▶ \(executable.lastPathComponent) \(args.joined(separator: " "))"))

        if let note = resolved.note {
            state.threadsStore.updateConsole(threadID: threadID, line: note)
        }
    }

    /// Sends immediate SIGINT to running process and parks the run.
    func stop() {
        guard isRunning else { return }
        stoppedByUser = true
        if let proc = process, proc.isRunning {
            proc.interrupt()
        }
    }

    // MARK: - Stream reading

    nonisolated private func readPipe(_ pipe: Pipe, isError: Bool, threadID: UUID) {
        let handle = pipe.fileHandleForReading
        handle.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty else { return }
            guard let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async {
                self?.consumeStream(text, isError: isError, threadID: threadID)
            }
        }
    }

    private var lineBuffer: [Bool: String] = [:]

    private func consumeStream(_ chunk: String, isError: Bool, threadID: UUID) {
        guard let state, runThreadID == threadID else { return }
        let current = (lineBuffer[isError] ?? "") + chunk
        let parts = current.components(separatedBy: "\n")
        let remainder = parts.last ?? ""
        if remainder.count > 8192 {
            state.threadsStore.updateConsole(threadID: threadID, line: "[Long unterminated output truncated] " + String(remainder.prefix(8100)))
            lineBuffer[isError] = ""
        } else {
            lineBuffer[isError] = remainder
        }

        for line in parts.dropLast() {
            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { continue }

            if !isError && trimmed.hasPrefix(eventPrefix) {
                let payload = String(trimmed.dropFirst(eventPrefix.count))
                handleEventLine(payload, threadID: threadID)
            } else {
                state.threadsStore.updateConsole(threadID: threadID, line: trimmed)
            }
        }
    }

    private func handleEventLine(_ jsonString: String, threadID: UUID) {
        guard let state else { return }
        let event: IDEEvent
        do { event = try IDEEvent.decode(jsonString) }
        catch { state.threadsStore.updateConsole(threadID: threadID, line: "Invalid @@CU event: \(error.localizedDescription)"); return }
        if event.type == "stats" {
            state.threadsStore.updateRunTelemetry(threadID: threadID, telemetry: RunTelemetry(
                totalTokens: Int(event.fields["total_tokens"]?.number ?? 0),
                costUSD: event.fields["cost_usd"]?.number,
                calls: Int(event.fields["calls"]?.number ?? 0),
                elapsedSeconds: event.fields["elapsed_seconds"]?.number ?? 0))
            return
        }
        if !event.plan.isEmpty {
            state.threadsStore.updatePlan(threadID: threadID, steps: event.plan, goal: event.planGoal)
        }
        let signature = event.subGoal + "|" + (event.action["type"]?.text ?? "")
        if event.error != nil {
            consecutiveFailures = signature == failureSignature ? consecutiveFailures + 1 : 1
            failureSignature = signature
        } else if event.type == "step" {
            consecutiveFailures = 0
            failureSignature = ""
        }
        if let notice = recoveryNotice(event: event, consecutiveFailures: consecutiveFailures) {
            state.threadsStore.appendTimeline(threadID: threadID, entry: TimelineEntry(kind: .action,
                title: notice, detail: event.error ?? "Recovery event", isFailure: event.error != nil))
        }
        guard event.type == "step" else { return }
        stepCount += 1
        let thinking = TimelineEntry(kind: .thinking,
            title: event.subGoal.isEmpty ? "thinking" : event.subGoal,
            detail: event.fields["thought"]?.text ?? "", isExpanded: true, isFailure: false)
        state.threadsStore.appendTimeline(threadID: threadID, entry: thinking)
        state.threadsStore.collapseOlderThinking(threadID: threadID, except: thinking.id)
        if !event.action.isEmpty {
            let action = event.action
            let type = action["type"]?.text ?? "action"
            if type == "load_skill" && event.error == nil {
                let skillID = action["skill_id"]?.text ?? "skill"
                let name = StoreDataService.shared.skills.first(where: { $0.id == skillID })?.name ?? skillID
                state.threadsStore.appendTimeline(threadID: threadID, entry: TimelineEntry(kind: .action,
                    title: "Skill yüklendi: " + name, detail: "internal_skill"))
            }
            let detail = event.error ?? "route: " + (event.fields["route"]?.text ?? "physical")
            state.threadsStore.appendTimeline(threadID: threadID, entry: TimelineEntry(kind: .action,
                title: type, detail: detail, isFailure: event.error != nil))
        }
        if let plan = state.threadsStore.threads.first(where: { $0.id == threadID })?.entries.last(where: { $0.kind == .plan }) {
            state.threadsStore.updatePlan(threadID: threadID, steps: plan.planSteps)
        }
    }

    private func handleTermination() {
        guard let state, let id = runThreadID else { return }
        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe?.fileHandleForReading.readabilityHandler = nil

        for (isErr, rest) in lineBuffer {
            let t = rest.trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty {
                if !isErr && t.hasPrefix(eventPrefix) {
                    handleEventLine(String(t.dropFirst(eventPrefix.count)), threadID: id)
                } else {
                    state.threadsStore.updateConsole(threadID: id, line: t)
                }
            }
        }
        lineBuffer.removeAll()

        let code = process?.terminationStatus ?? -1
        let latency = Date().timeIntervalSince(runStartedAt)
        let success = code == 0 && !stoppedByUser
        let summary: String
        if stoppedByUser {
            summary = "stopped by user (SIGINT) · \(stepCount) steps"
        } else if code == 0 {
            summary = "completed · \(stepCount) steps"
        } else {
            summary = "exit code \(code) · \(stepCount) steps"
        }

        isRunning = false
        process = nil
        runThreadID = nil
        if !success {
            state.threadsStore.appendTimeline(
                threadID: id,
                entry: TimelineEntry(
                    kind: .action, title: stoppedByUser ? "parked" : "run failed",
                    detail: summary, isFailure: !stoppedByUser))
        }
        StoreDataService.shared.loadControlRecords()
        state.runnerDidFinish(
            threadID: id, success: success, summary: summary,
            latency: latency, stopped: stoppedByUser)
    }

    // MARK: - Command resolution (pure)

    /// A resolved CLI invocation: the interpreter executable, its arguments,
    /// and an optional console hint shown next to the "running" line (nil
    /// when the resolution is fully normal).
    struct ResolvedCommand {
        let executable: URL
        let args: [String]
        let note: String?
    }

    /// Resolves the interpreter that runs `python -m computeruse`, in order:
    /// 1. `COMPUTERUSE_CLI` — full invocation override (e.g. a wrapper
    ///    script); CLI args are appended verbatim.
    /// 2. `COMPUTERUSE_PYTHON` — explicit interpreter path.
    /// 3. The repo virtualenv (`.venv/bin/python3`) — from `COMPUTERUSE_REPO`
    ///    or discovered around the bundle / working directory. This is the
    ///    interpreter that actually has `computeruse` installed.
    /// 4. System `/usr/bin/python3` — last resort, almost certainly missing
    ///    the module, so it carries a fix-it hint.
    static func resolveCommand(cliArgs: [String]) -> ResolvedCommand {

        if let explicit = ProcessInfo.processInfo.environment["COMPUTERUSE_CLI"],
            !explicit.isEmpty
        {
            return ResolvedCommand(
                executable: URL(fileURLWithPath: explicit),
                args: cliArgs, note: nil)
        }

        if let explicit = ProcessInfo.processInfo.environment["COMPUTERUSE_PYTHON"],
            !explicit.isEmpty,
            FileManager.default.isExecutableFile(atPath: explicit)
        {
            return ResolvedCommand(
                executable: URL(fileURLWithPath: explicit),
                args: ["-m", "computeruse"] + cliArgs,
                note: nil)
        }

        if let venvPython = Self.venvPython() {
            return ResolvedCommand(
                executable: venvPython,
                args: ["-m", "computeruse"] + cliArgs,
                note: nil)
        }

        let sysPython = URL(fileURLWithPath: "/usr/bin/python3")
        return ResolvedCommand(
            executable: sysPython,
            args: ["-m", "computeruse"] + cliArgs,
            note:
                "'computeruse' is not installed for system python3. Run `uv sync` "
                + "in the repo, or set COMPUTERUSE_PYTHON / COMPUTERUSE_REPO.")
    }

    /// Locates `.venv/bin/python3` inside the repository by walking upward
    /// from a set of anchors until a directory that both looks like the repo
    /// (`pyproject.toml`) and carries an executable venv interpreter is
    /// found. Returns nil when no usable venv exists anywhere.
    ///
    /// Anchor order:
    /// 1. `COMPUTERUSE_REPO` environment variable (explicit override).
    /// 2. The compile-time path of this source file — absolute in local
    ///    SwiftPM/Xcode builds, so it still names the repo when the bundle
    ///    lives in DerivedData (the Xcode dev flow). Falls back to the
    ///    process cwd when the compiler emitted a package-relative path.
    /// 3. The app bundle itself (packaged layout: `<repo>/desktop/
    ///    ComputerUseDesktop.app`).
    /// 4. The process working directory (`swift run` from the repo).
    ///
    /// The old bundle-only root is why Xcode launches fell back to
    /// `/usr/bin/python3` ("No module named computeruse"): the bundle sits in
    /// DerivedData, nowhere near the repo, so `.venv` could never be found.
    private static func venvPython() -> URL? {
        var anchors: [URL] = []
        if let repo = ProcessInfo.processInfo.environment["COMPUTERUSE_REPO"],
            !repo.isEmpty
        {
            anchors.append(URL(fileURLWithPath: repo).resolvingSymlinksInPath())
        }

        let sourcePath = String(#filePath)
        if sourcePath.hasPrefix("/") {
            anchors.append(URL(fileURLWithPath: sourcePath).resolvingSymlinksInPath())
        } else {
            // Package-relative compile-time path: resolve against the cwd.
            anchors.append(
                URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
                    .appendingPathComponent(sourcePath)
                    .resolvingSymlinksInPath())
        }

        anchors.append(Bundle.main.bundleURL.resolvingSymlinksInPath())
        anchors.append(
            URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
                .resolvingSymlinksInPath())

        for anchor in anchors {
            var dir = anchor.standardizedFileURL
            // Cap the upward walk: a repo is never deeper than a few levels
            // below home, and `/` must not be scanned.
            for _ in 0..<8 {
                let venv = dir.appendingPathComponent(".venv/bin/python3")
                if FileManager.default.isExecutableFile(atPath: venv.path),
                    FileManager.default.fileExists(
                        atPath: dir.appendingPathComponent("pyproject.toml").path)
                {
                    return venv
                }
                let parent = dir.deletingLastPathComponent().standardizedFileURL
                guard parent.path != dir.path else { break }  // filesystem root
                dir = parent
            }
        }
        return nil
    }

    /// The repository root (parent of `.venv`), or the app's own directory as
    /// a last resort so the child process still starts with a sane cwd.
    static func repoRoot() -> URL {
        if let venv = venvPython() {
            return venv.deletingLastPathComponent()
                .deletingLastPathComponent()
                .deletingLastPathComponent()
        }
        return Bundle.main.bundleURL.deletingLastPathComponent()
    }

    func buildCliArgs(goal: String, state: AppState) throws -> [String] {
        var args: [String] = [
            "--goal", goal,
            "--model", AgentModel.cliSpec(for: state.selectedModel),
            "--level", "\(state.autonomyLevel.cliLevel)",
        ]
        args += try state.safetyOptions.arguments()
        if state.trustMode {
            args.append("--yes")
        }
        if state.mcpEnabled {
            args.append("--mcp")
        }
        if let app = state.targetApp, !app.isEmpty {
            args.append(contentsOf: ["--app", app])
        }
        if state.useRealDriver {
            args.append("--real")
            if let bin = Self.resolveDriverBinary() {
                args.append(contentsOf: ["--driver", bin.path])
            }
        }
        return args
    }

    static func resolveDriverBinary() -> URL? {
        if let explicit = ProcessInfo.processInfo.environment["COMPUTERUSE_DRIVER_BIN"],
            !explicit.isEmpty
        {
            let url = URL(fileURLWithPath: explicit)
            if FileManager.default.isExecutableFile(atPath: url.path) { return url }
        }
        let root = repoRoot()
        let releaseBin = root.appendingPathComponent("driver/target/release/actuation-driver")
        if FileManager.default.isExecutableFile(atPath: releaseBin.path) { return releaseBin }
        let debugBin = root.appendingPathComponent("driver/target/debug/actuation-driver")
        if FileManager.default.isExecutableFile(atPath: debugBin.path) { return debugBin }
        return nil
    }

}
