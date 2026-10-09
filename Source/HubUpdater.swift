import AppKit
import SwiftUI

struct HubUpdateInfo: Decodable, Sendable {
    var version: String
    var build: String
    var tag: String?
    var path: String?
    var target: String?
    var script: String?
    var installedVersion: String?
    var installedBuild: String?
}

enum HubUpdateRestartPolicy {
    static func blocker(desktopOpen: Bool, activeWork: Bool, unsavedSettings: Bool, drafts: Bool) -> String? {
        if desktopOpen { return "Close Codex / ChatGPT and Claude, then click Restart." }
        if activeWork { return "Wait for the current work to finish, then click Restart." }
        if unsavedSettings { return "Save your settings, then click Restart." }
        if drafts { return "Send or clear your chat drafts, then click Restart." }
        return nil
    }
}

/// One updater shared by both mastheads. Downloads never replace live worker
/// resources; the installer runs only after the normal shutdown path succeeds.
@MainActor
final class HubUpdater: ObservableObject {
    static let shared = HubUpdater()
    @Published private(set) var available: HubUpdateInfo?
    @Published private(set) var ready: HubUpdateInfo?
    @Published private(set) var working = false
    @Published private(set) var hint = ""
    @Published var error: String?
    private(set) var restartRequested = false
    private var installer: HubUpdateInfo?
    private var python: String?
    private var helper: URL?
    private var cache: URL?
    private var blocker: () -> String? = { "The app is still starting." }
    private var checking = false
    private var timer: Timer?
    private let execute: ((String, String?) async throws -> HubUpdateInfo?)?
    private let restartApp: @MainActor () -> Void

    /// Exercise the real state machine without downloads or terminating a test
    /// runner. Production uses the bundled helper and normal AppKit shutdown.
    init(execute: ((String, String?) async throws -> HubUpdateInfo?)? = nil,
         restart: @escaping @MainActor () -> Void = { NSApp.terminate(nil) },
         blocker: @escaping () -> String? = { "The app is still starting." }) {
        self.execute = execute; self.restartApp = restart; self.blocker = blocker
    }

    var visible: Bool { available != nil || ready != nil || working }
    var label: String { working ? "Updating…" : ready != nil ? "Restart" : "Update" }
    var help: String {
        if !hint.isEmpty { return hint }
        let update = ready ?? available
        return update.map { "Update to Provider Hub \($0.version) (build \($0.build))" } ?? "Check for updates"
    }

    func start(python: String?, worker: URL, root: URL, blocker: @escaping () -> String?) {
        guard helper == nil else { return }
        self.python = python
        helper = worker.appendingPathComponent("hub_updater.py")
        cache = root.appendingPathComponent("updates", isDirectory: true)
        self.blocker = blocker
        Task {
            ready = try? await run("pending")
            if ready != nil { hint = "Update ready. Click Restart when your work is finished." }
            await check()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 6 * 60 * 60, repeats: true) { [weak self] _ in
            guard let owner = self else { return }
            Task { await owner.check() }
        }
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification,
            object: nil, queue: .main) { [weak self] _ in
                guard let owner = self else { return }
                Task { await owner.check() }
            }
    }

    func check() async {
        guard !checking, !working, ready == nil, helper != nil || execute != nil else { return }
        checking = true; defer { checking = false }
        // Offline launches and GitHub rate limits remain quiet. An advertised
        // update stays available for retry until a successful check changes it.
        do { available = try await run("check") } catch {}
    }

    func activate() async {
        guard !working else { return }
        if ready != nil { await requestRestart(); return }
        guard let tag = available?.tag else { return }
        let deferRestart = blocker() != nil
        working = true; hint = "Downloading and verifying the update…"
        do {
            ready = try await run("stage", tag: tag)
            guard ready != nil else { throw WorkerError(message: "This update is no longer available.") }
            available = nil; working = false
            if deferRestart || blocker() != nil {
                hint = "Update ready. " + (blocker() ?? "Click Restart when you are ready.")
            } else {
                await requestRestart()
            }
        } catch {
            working = false; hint = "Click Update to retry."; self.error = error.localizedDescription
        }
    }

    func requestRestart() async {
        guard ready != nil, !working else { return }
        if let reason = blocker() { hint = "Update ready. " + reason; error = reason; return }
        working = true
        do {
            installer = try await run("prepare")
            working = false
            if let reason = blocker() { deferRestart(reason); return }
            restartRequested = true
            restartApp()
        } catch { working = false; self.error = error.localizedDescription }
    }

    func deferRestart(_ reason: String) {
        restartRequested = false; installer = nil
        hint = "Update ready. " + reason
    }

    /// Called at the last step of successful graceful shutdown, immediately
    /// before terminateLater is acknowledged. The helper waits for this PID.
    func launchInstaller() throws {
        guard restartRequested else { return }
        guard let installer, let script = installer.script, let target = installer.target,
              let path = installer.path, let version = installer.installedVersion,
              let build = installer.installedBuild, let cache else {
            throw WorkerError(message: "The update installer is not ready. Click Restart to retry.")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = [script, String(ProcessInfo.processInfo.processIdentifier), target, path, version, build]
        let log = cache.appendingPathComponent("install.log")
        FileManager.default.createFile(atPath: log.path, contents: nil)
        let output = try FileHandle(forWritingTo: log)
        process.standardOutput = output; process.standardError = output
        process.standardInput = FileHandle.nullDevice
        try process.run()
        try? output.close()
    }

    private func run(_ action: String, tag: String? = nil) async throws -> HubUpdateInfo? {
        if let execute { return try await execute(action, tag) }
        guard let python, let helper, let cache,
              let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
              let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String else {
            throw WorkerError(message: "A bundled Python runtime is needed to update Provider Hub.")
        }
        var arguments = [helper.path, action, "--version", version, "--build", build,
            "--cache", cache.path, "--target", Bundle.main.bundleURL.resolvingSymlinksInPath().path]
        if let tag { arguments += ["--tag", tag] }
        let commandArguments = arguments
        return try await Task.detached {
            let process = Process(), output = Pipe()
            process.executableURL = URL(fileURLWithPath: python); process.arguments = commandArguments
            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            process.environment = environment
            process.standardOutput = output; process.standardError = FileHandle.nullDevice
            process.standardInput = FileHandle.nullDevice
            try process.run()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            struct Reply: Decodable { var result: HubUpdateInfo?; var error: String? }
            guard let reply = try? JSONDecoder().decode(Reply.self, from: data) else {
                throw WorkerError(message: "The update helper could not complete. Click Update to retry.")
            }
            if process.terminationStatus != 0 || reply.error != nil {
                throw WorkerError(message: reply.error ?? "The update could not complete.")
            }
            return reply.result
        }.value
    }
}

@MainActor
struct HubUpdatePill: View {
    @ObservedObject var updater = HubUpdater.shared
    @State private var showingError = false
    var body: some View {
        if updater.visible {
            Button(updater.label) { Task { await updater.activate(); showingError = updater.error != nil } }
                .font(.system(size: 10.5, weight: .semibold))
                .foregroundStyle(HubTheme.Semantic.ink)
                .padding(.horizontal, 9).padding(.vertical, 3)
                .background(HubTheme.Accent.brand.opacity(0.16), in: Capsule())
                .overlay(Capsule().stroke(HubTheme.Accent.brand.opacity(0.3), lineWidth: 0.5))
                .buttonStyle(.plain).disabled(updater.working).help(updater.help)
                .accessibilityLabel(updater.ready == nil ? "Update Provider Hub" : "Restart Provider Hub to finish updating")
                .alert("Could not update Provider Hub", isPresented: $showingError) {
                    Button("OK", role: .cancel) { updater.error = nil }
                } message: { Text(updater.error ?? "") }
        }
    }
}
