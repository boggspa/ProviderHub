import AppKit
import SwiftUI
import Security

private let bridgeOrange = Color(red: 1.0, green: 0.43, blue: 0.18)
private let slots: [(id: String, label: String)] = [
    ("claude-fable-5", "Fable 5"), ("claude-opus-5", "Opus 5"),
    ("claude-sonnet-5", "Sonnet 5"), ("claude-haiku-4-5", "Haiku 4.5"), ("claude-sonnet-4-6", "Sonnet 4.6")
]

struct RouteSettings: Codable, Equatable {
    var port = 11436
    var credential_mode = "vibe"
    var mappings = Dictionary(uniqueKeysWithValues: slots.map { ($0.id, "mistral-vibe-cli-latest") })
    var auto_stop = true
    var auto_mode = false
}

struct ModelEntry: Decodable, Identifiable {
    var id: String
    var canonical_id: String?
    var display_name: String?
    var context: Int?
    var tools: Bool?
    var vision: Bool?
    var reasoning: Bool?
    var aliases: [String]?
    var inference_status: String?
    var last_success: String?
}

struct ActivityEntry: Identifiable {
    let id = UUID()
    var time: String
    var event: String
    var model: String
    var status: Int?
}

enum Page: String, CaseIterable, Identifiable {
    case connection = "Connection", models = "Models", claude = "Claude", activity = "Activity"
    var id: String { rawValue }
    var icon: String {
        switch self { case .connection: return "point.3.connected.trianglepath.dotted"; case .models: return "square.stack.3d.up"; case .claude: return "macwindow"; case .activity: return "waveform.path" }
    }
}

struct WorkerError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

@MainActor
final class BridgeModel: ObservableObject {
    @Published var page: Page = .connection
    @Published var settings = RouteSettings()
    @Published var savedSettings = RouteSettings()
    @Published var busy = false
    @Published var gatewayState = "Stopped"
    @Published var credentialSource = "Checking Vibe…"
    @Published var credentialFound = false
    @Published var vibeAlias = ""
    @Published var vibeModel = "mistral-vibe-cli-latest"
    @Published var vibeConfigured: [String] = []
    @Published var availableModels: [ModelEntry] = []
    @Published var friendlyNames: [String: String] = [:]
    @Published var catalogueSummary = "Refresh models to load provider limits."
    @Published var claudeInstalled = false
    @Published var claudeRunning = false
    @Published var profileActive = false
    @Published var recoveryNeeded = false
    @Published var activeRequests = 0
    @Published var completed = 0
    @Published var failures = 0
    @Published var inputTokens = 0
    @Published var outputTokens = 0
    @Published var activity: [ActivityEntry] = []
    @Published var notice = ""
    @Published var noticeIsError = false
    @Published var secretDraft = ""
    @Published var showRoutingIDs = false
    @Published var runtimeFound = true
    var gatewayProcess: Process?
    var parentPipe: Pipe?
    var processBuffer = Data()
    var pollTimer: Timer?
    var checkingStatus = false
    var finishingSession = false
    var hasObservedOwnedClaude = false
    var lastLaunchTime = Date.distantPast
    let root: URL
    let helper: URL
    var python: String?
    var shuttingDown = false

    init() {
        root = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support/Mistral Bridge", isDirectory: true)
        helper = Bundle.main.resourceURL!.appendingPathComponent("worker/gateway.py")
        python = Self.findPython()
        runtimeFound = python != nil
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.poll() }
        }
        Task { await reconnect(recover: true) }
    }

    var running: Bool { gatewayState == "Ready" }
    var changed: Bool { settings != savedSettings }
    var routeOptions: [String] {
        let known = Set(availableModels.flatMap { ($0.aliases ?? []) + [$0.id] })
        let extra = Set(settings.mappings.values.filter { !known.contains($0) })
        return availableModels.map(\.id) + extra.sorted()
    }
    var endpoint: String { "http://127.0.0.1:\(savedSettings.port)" }

    func modelLabel(_ identifier: String) -> String { friendlyNames[identifier] ?? identifier }

    func modelEntry(_ identifier: String) -> ModelEntry? {
        availableModels.first { $0.id == identifier || ($0.aliases ?? []).contains(identifier) }
    }

    func modelFacts(_ identifier: String) -> String {
        guard let entry = modelEntry(identifier), let context = entry.context else { return "Metadata missing · refresh models" }
        let state: String
        switch entry.inference_status {
        case "responded": state = "Previously responded"
        case "quota_limited": state = "Quota or rate limited"
        case "unavailable": state = "Rejected by provider"
        default: state = "Advertised · not tested"
        }
        return "\(context.formatted()) tokens · \(state)"
    }

    func readCatalogue(_ object: [String: Any], modelsKey: String) {
        if let raw = object[modelsKey], let data = try? JSONSerialization.data(withJSONObject: raw) {
            availableModels = (try? JSONDecoder().decode([ModelEntry].self, from: data)) ?? []
        }
        friendlyNames = object["friendly_names"] as? [String: String] ?? friendlyNames
        if let summary = object["catalog_summary"] as? [String: Any] {
            if summary["needs_refresh"] as? Bool == true { catalogueSummary = "Refresh models to load provider limits and aliases." }
            else { catalogueSummary = "\(availableModels.count) models · \(summary["aliases_grouped"] as? Int ?? 0) aliases grouped · \(summary["retired_ids"] as? Int ?? 0) marked retired" }
        }
    }

    static func findVibe() -> String? {
        let candidates = [NSHomeDirectory() + "/.local/bin/vibe", "/opt/homebrew/bin/vibe", "/usr/local/bin/vibe"]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    static func findPython() -> String? {
        if let vibe = findVibe(), let first = try? String(contentsOfFile: vibe, encoding: .utf8).components(separatedBy: .newlines).first,
           first.hasPrefix("#!/"), !first.contains("/usr/bin/env") {
            let candidate = String(first.dropFirst(2)).trimmingCharacters(in: .whitespaces)
            if candidate.contains("python"), FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        let candidates = [NSHomeDirectory() + "/.local/share/uv/tools/mistral-vibe/bin/python3", "/opt/homebrew/bin/python3.13", "/opt/homebrew/bin/python3.12"]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    nonisolated static func execute(python: String, helper: String, root: String, command: String, input: Data?) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: python)
                process.arguments = [helper, command]
                var env = ProcessInfo.processInfo.environment
                env["MISTRAL_BRIDGE_STATE_DIR"] = root
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                env["PYTHONUNBUFFERED"] = "1"
                process.environment = env
                let output = Pipe(), errors = Pipe(), stdin = Pipe()
                process.standardOutput = output; process.standardError = errors; process.standardInput = stdin
                do {
                    try process.run()
                    if let input { try stdin.fileHandleForWriting.write(contentsOf: input) }
                    try stdin.fileHandleForWriting.close()
                    DispatchQueue.global().asyncAfter(deadline: .now() + 45) {
                        if process.isRunning { process.terminate() }
                    }
                    let data = output.fileHandleForReading.readDataToEndOfFile()
                    _ = errors.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()
                    if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any], object["ok"] as? Bool == false {
                        throw WorkerError(message: object["error"] as? String ?? "The operation failed.")
                    }
                    guard process.terminationStatus == 0 else { throw WorkerError(message: "The Vibe runtime could not complete this operation. Reconnect and try again.") }
                    continuation.resume(returning: data)
                } catch { continuation.resume(throwing: error) }
            }
        }
    }

    func command(_ name: String, input: Data? = nil) async throws -> [String: Any] {
        guard let python else { throw WorkerError(message: "Install or update Mistral Vibe, then click Reconnect.") }
        let data = try await Self.execute(python: python, helper: helper.path, root: root.path, command: name, input: input)
        return (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    func tell(_ message: String, error: Bool = false) {
        notice = message; noticeIsError = error
    }

    func reconnect(recover: Bool = false) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        python = Self.findPython(); runtimeFound = python != nil
        do {
            let info = try await command("inspect")
            if let raw = info["settings"], let data = try? JSONSerialization.data(withJSONObject: raw), let decoded = try? JSONDecoder().decode(RouteSettings.self, from: data) {
                settings = decoded; savedSettings = decoded
            }
            credentialFound = info["credential_found"] as? Bool ?? false
            credentialSource = info["credential_source"] as? String ?? "Not connected"
            claudeInstalled = info["claude_installed"] as? Bool ?? false
            claudeRunning = info["claude_running"] as? Bool ?? false
            profileActive = info["profile_active"] as? Bool ?? false
            recoveryNeeded = info["recovery_needed"] as? Bool ?? false
            if let vibe = info["vibe"] as? [String: Any] {
                vibeAlias = vibe["active_display_name"] as? String ?? vibe["active_alias"] as? String ?? ""
                vibeModel = vibe["active_model"] as? String ?? "mistral-vibe-cli-latest"
                vibeConfigured = vibe["configured_models"] as? [String] ?? []
            }
            readCatalogue(info, modelsKey: "catalog")
            if recover && recoveryNeeded && !claudeRunning {
                _ = try await command("restore")
                recoveryNeeded = false; profileActive = false
                tell("Recovered the previous Claude configuration after an interrupted launch.")
            } else if recover && recoveryNeeded && profileActive && claudeRunning {
                try await startGateway()
                hasObservedOwnedClaude = true
                tell("Reconnected the gateway for the existing Mistral Claude session.")
            } else if !credentialFound {
                tell(info["credential_error"] as? String ?? "Sign in through Vibe, then reconnect.", error: true)
            } else if !recover { tell("Connected to \(credentialSource). Vibe’s current model is \(vibeAlias).") }
        } catch { tell(error.localizedDescription, error: true) }
        loadActivity()
    }

    func discover() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do {
            let result = try await command("discover")
            readCatalogue(result, modelsKey: "models")
            tell("\(catalogueSummary). Limits come from Mistral. Advertised models are not marked tested until a request succeeds.")
        } catch { tell(error.localizedDescription, error: true) }
    }

    func save() async throws {
        if gatewayProcess?.isRunning == true && (activeRequests > 0 || (claudeRunning && profileActive)) {
            throw WorkerError(message: "Quit the Mistral Claude session before changing its active configuration.")
        }
        if gatewayProcess?.isRunning == true { await stopGateway() }
        let data = try JSONEncoder().encode(settings)
        let result = try await command("save", input: data)
        readCatalogue(result, modelsKey: "models")
        savedSettings = settings
    }

    func saveFromUI() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do { try await save(); tell("Settings saved. Your next Claude launch will use these mappings.") }
        catch { tell(error.localizedDescription, error: true) }
    }

    func startGateway() async throws {
        if running { return }
        guard gatewayProcess == nil else { throw WorkerError(message: "The gateway is still starting or stopping.") }
        guard let python else { throw WorkerError(message: "Mistral Vibe’s Python runtime was not found.") }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [helper.path, "serve", "--parent-pipe"]
        var env = ProcessInfo.processInfo.environment
        env["MISTRAL_BRIDGE_STATE_DIR"] = root.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        process.environment = env
        let stdout = Pipe(), stderr = Pipe(), stdin = Pipe()
        process.standardOutput = stdout; process.standardError = stderr; process.standardInput = stdin
        parentPipe = stdin; processBuffer = Data(); gatewayState = "Starting"
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            Task { @MainActor in self?.consumeOutput(data) }
        }
        stderr.fileHandleForReading.readabilityHandler = { handle in
            // Worker errors are returned as redacted JSON on stdout.
            if handle.availableData.isEmpty { handle.readabilityHandler = nil }
        }
        process.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                guard let self, self.gatewayProcess === process else { return }
                self.gatewayProcess = nil; self.parentPipe = nil
                if self.gatewayState != "Error" { self.gatewayState = "Stopped" }
            }
        }
        do { try process.run(); gatewayProcess = process }
        catch { gatewayState = "Error"; parentPipe = nil; throw error }
        for _ in 0..<150 {
            if running { return }
            if gatewayState == "Error" || !process.isRunning { break }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        if !running {
            if process.isRunning { process.terminate() }
            throw WorkerError(message: noticeIsError ? notice : "The gateway could not start. Reconnect to Vibe and check the port.")
        }
    }

    func consumeOutput(_ data: Data) {
        processBuffer.append(data)
        while let range = processBuffer.range(of: Data([10])) {
            let line = processBuffer.subdata(in: 0..<range.lowerBound)
            processBuffer.removeSubrange(0..<range.upperBound)
            guard let json = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            if json["ready"] as? Bool == true {
                gatewayState = "Ready"; credentialFound = true
                credentialSource = json["credential_source"] as? String ?? credentialSource
            } else if let error = json["error"] as? String {
                gatewayState = "Error"; tell(error, error: true)
            }
        }
    }

    func stopGateway() async {
        guard let process = gatewayProcess else { return }
        gatewayState = "Stopping"
        try? parentPipe?.fileHandleForWriting.close()
        parentPipe = nil
        for _ in 0..<30 {
            if !process.isRunning { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        if process.isRunning { process.terminate() }
        gatewayProcess = nil; gatewayState = "Stopped"; activeRequests = 0
    }

    func toggleGateway() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do {
            if running {
                if (claudeRunning && profileActive) || activeRequests > 0 { throw WorkerError(message: "Close the Mistral Claude session before stopping its gateway.") }
                await stopGateway()
                tell("Gateway stopped. Your model mappings are saved for the next launch.")
            } else { try await save(); try await startGateway(); tell("Gateway ready. You can test a model or launch Claude.") }
        } catch { tell(error.localizedDescription, error: true) }
    }

    func authorizedRequest(path: String, method: String = "GET", body: [String: Any]? = nil) throws -> URLRequest {
        let token = try String(contentsOf: root.appendingPathComponent("gateway-token"), encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        var request = URLRequest(url: URL(string: endpoint + path)!)
        request.httpMethod = method
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 90
        if let body { request.httpBody = try JSONSerialization.data(withJSONObject: body); request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        return request
    }

    func testRoute(_ slot: String = "claude-fable-5") async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do {
            if changed { try await save() }
            try await startGateway()
            let request = try authorizedRequest(path: "/v1/messages", method: "POST", body: [
                "model": slot, "max_tokens": 96, "thinking": ["type": "disabled"],
                "messages": [["role": "user", "content": "Reply with exactly: Mistral Bridge is connected."]]
            ])
            let (data, response) = try await URLSession.shared.data(for: request)
            let object = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
            if (response as? HTTPURLResponse)?.statusCode != 200 {
                let error = object["error"] as? [String: Any]
                throw WorkerError(message: error?["message"] as? String ?? "The model test failed.")
            }
            let content = object["content"] as? [[String: Any]] ?? []
            let reply = content.compactMap { $0["text"] as? String }.joined()
            guard !reply.isEmpty else { throw WorkerError(message: "The route responded but returned no visible text. Try a different model or reasoning level.") }
            tell("Connected · \(modelLabel(savedSettings.mappings[slot] ?? slot))\n\(reply)")
        } catch { tell(error.localizedDescription, error: true) }
        await refreshStatus()
        if let result = try? await command("inspect") { readCatalogue(result, modelsKey: "catalog") }
    }

    func launchClaude() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        updateClaudeRunning()
        if claudeRunning {
            if profileActive {
                NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == "com.anthropic.claudefordesktop" }?.activate(options: [.activateAllWindows])
            } else { tell("Claude is already open with another profile. Finish your current work and quit Claude, then launch it here.", error: true) }
            return
        }
        do {
            if recoveryNeeded { _ = try await command("restore"); recoveryNeeded = false }
            try await save()
            try await startGateway()
            _ = try await command("activate")
            profileActive = true; recoveryNeeded = true
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            // Profile isolation uses Claude's native 3P mode. Do not change HOME,
            // Chromium flags, the application bundle, or the user's CLI config.
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: "/Applications/Claude.app"), configuration: configuration) { _, error in
                    if let error { continuation.resume(throwing: error) }
                    else { continuation.resume() }
                }
            }
            lastLaunchTime = Date(); hasObservedOwnedClaude = false
            tell("Claude is opening with your Mistral profile. Its previous configuration will be restored after Claude quits.")
        } catch {
            updateClaudeRunning()
            if !claudeRunning && recoveryNeeded {
                _ = try? await command("restore")
                recoveryNeeded = false; profileActive = false
            }
            tell(error.localizedDescription, error: true)
        }
    }

    func restore() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do {
            let result = try await command("restore")
            recoveryNeeded = false; profileActive = false; hasObservedOwnedClaude = false
            await stopGateway()
            let kept = result["preserved_external_changes"] as? Int ?? 0
            tell(kept > 0 ? "Disconnected. Changes made by another app were preserved." : "Restored Claude’s previous configuration. Your conversations remain saved.")
        } catch { tell(error.localizedDescription, error: true) }
    }

    func updateClaudeRunning() {
        claudeRunning = NSWorkspace.shared.runningApplications.contains { $0.bundleIdentifier == "com.anthropic.claudefordesktop" && !$0.isTerminated }
    }

    func poll() async {
        guard !shuttingDown else { return }
        updateClaudeRunning()
        let metaPath = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support/Claude-3p/configLibrary/_meta.json")
        if let data = try? Data(contentsOf: metaPath), let meta = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            profileActive = meta["appliedId"] as? String == "8a93d471-d0f9-428c-b203-48fce46277bc"
        }
        if profileActive && claudeRunning { hasObservedOwnedClaude = true }
        let launchFailed = profileActive && !claudeRunning && !hasObservedOwnedClaude && Date().timeIntervalSince(lastLaunchTime) > 20
        if recoveryNeeded && !claudeRunning && (hasObservedOwnedClaude || launchFailed) && !busy && !finishingSession {
            finishingSession = true
            do {
                _ = try await command("restore")
                recoveryNeeded = false; profileActive = false; hasObservedOwnedClaude = false
                if savedSettings.auto_stop { await stopGateway() }
                tell("Claude closed. Its previous configuration has been restored.")
            } catch { tell(error.localizedDescription, error: true) }
            finishingSession = false
        }
        if running && !checkingStatus { await refreshStatus() }
        loadActivity()
    }

    func refreshStatus() async {
        guard running, !checkingStatus else { return }
        checkingStatus = true; defer { checkingStatus = false }
        do {
            var request = try authorizedRequest(path: "/_bridge/status")
            request.timeoutInterval = 2
            let (data, _) = try await URLSession.shared.data(for: request)
            guard let status = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            activeRequests = status["active"] as? Int ?? 0; completed = status["completed"] as? Int ?? 0
            failures = status["failed"] as? Int ?? 0; inputTokens = status["input_tokens"] as? Int ?? 0
            outputTokens = status["output_tokens"] as? Int ?? 0
        } catch { }
    }

    func loadActivity() {
        guard let text = try? String(contentsOf: root.appendingPathComponent("activity.jsonl"), encoding: .utf8) else { return }
        let entries = text.split(separator: "\n").suffix(30).reversed().compactMap { line -> ActivityEntry? in
            guard let raw = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] else { return nil }
            return ActivityEntry(time: raw["time"] as? String ?? "", event: raw["event"] as? String ?? "", model: raw["model"] as? String ?? "", status: raw["status"] as? Int)
        }
        // Avoid replacing row identities on every timer tick.
        if entries.count != activity.count || entries.first?.time != activity.first?.time || entries.first?.event != activity.first?.event { activity = entries }
    }

    func saveKey() async {
        let key = secretDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { tell("Enter your Mistral API key first.", error: true); return }
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "com.mistralbridge.local", kSecAttrAccount as String: "MISTRAL_API_KEY"]
        let attributes: [String: Any] = [kSecValueData as String: Data(key.utf8)]
        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var entry = query; entry.merge(attributes) { _, new in new }
            entry[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            status = SecItemAdd(entry as CFDictionary, nil)
        }
        guard status == errSecSuccess else { tell("Keychain could not save the key (\(status)).", error: true); return }
        secretDraft = ""; settings.credential_mode = "separate"
        await saveFromUI()
        await reconnect()
    }

    func openVibe() {
        guard let executable = Self.findVibe() else { tell("Mistral Vibe was not found. Install Vibe, then reconnect.", error: true); return }
        let shell = "'" + executable.replacingOccurrences(of: "'", with: "'\\''") + "'"
        let quoted = shell.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        let source = "tell application \"Terminal\"\nactivate\ndo script \"\(quoted)\"\nend tell"
        var error: NSDictionary?
        NSAppleScript(source: source)?.executeAndReturnError(&error)
        if error != nil { tell("Terminal could not open Vibe. Start Vibe normally, then reconnect.", error: true) }
    }
}

struct Panel<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 18) { content }
            .padding(22).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.07), lineWidth: 1))
    }
}

struct StatusPill: View {
    let text: String
    var good: Bool
    var body: some View {
        HStack(spacing: 6) { Circle().fill(good ? Color.green : Color.secondary).frame(width: 6, height: 6); Text(text).font(.system(size: 11, weight: .medium)) }
            .padding(.horizontal, 10).padding(.vertical, 6).background(Color.white.opacity(0.055), in: Capsule())
    }
}

struct BridgeWindow: View {
    @ObservedObject var model: BridgeModel
    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 28) {
                HStack(spacing: 10) {
                    Image(systemName: "point.3.connected.trianglepath.dotted").font(.system(size: 27, weight: .semibold)).foregroundStyle(bridgeOrange)
                    VStack(alignment: .leading, spacing: 2) { Text("Mistral").font(.system(size: 18, weight: .semibold)); Text("BRIDGE").font(.system(size: 10, weight: .semibold)).tracking(2).foregroundStyle(.secondary) }
                }.padding(.top, 12)
                VStack(spacing: 5) {
                    ForEach(Page.allCases) { page in
                        Button { model.page = page } label: {
                            HStack(spacing: 11) { Image(systemName: page.icon).frame(width: 18); Text(page.rawValue).lineLimit(1); Spacer() }
                                .font(.system(size: 13, weight: model.page == page ? .semibold : .regular))
                                .padding(.horizontal, 12).padding(.vertical, 10)
                                .background(model.page == page ? Color.white.opacity(0.09) : .clear, in: RoundedRectangle(cornerRadius: 8))
                        }.buttonStyle(.plain).foregroundStyle(model.page == page ? .primary : .secondary)
                    }
                }
                Spacer()
                VStack(alignment: .leading, spacing: 9) {
                    StatusPill(text: model.running ? "Gateway ready" : model.gatewayState, good: model.running)
                    Text("0.2.0 · LOCAL PROTOTYPE").font(.system(size: 9, weight: .medium)).tracking(1).foregroundStyle(.tertiary)
                }
            }.padding(20).frame(width: 200).background(Color.black.opacity(0.15))
            Rectangle().fill(Color.white.opacity(0.07)).frame(width: 1)
            VStack(spacing: 0) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 22) {
                        header
                        switch model.page {
                        case .connection: connectionPage
                        case .models: modelsPage
                        case .claude: claudePage
                        case .activity: activityPage
                        }
                    }.padding(30)
                }
                if !model.notice.isEmpty {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: model.noticeIsError ? "exclamationmark.circle" : "checkmark.circle").foregroundStyle(model.noticeIsError ? Color.orange : .green)
                        Text(model.notice).font(.system(size: 12)).lineSpacing(3).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                        Button { model.notice = "" } label: { Image(systemName: "xmark").font(.system(size: 10)) }.buttonStyle(.plain).foregroundStyle(.secondary)
                    }.padding(16).background(Color.white.opacity(0.045)).overlay(alignment: .top) { Divider() }
                }
            }.frame(maxWidth: .infinity)
        }.background(Color(red: 0.095, green: 0.098, blue: 0.102)).accentColor(bridgeOrange)
            .frame(minWidth: 870, idealWidth: 940, minHeight: 680, idealHeight: 740)
    }

    var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 7) {
                Text(model.page.rawValue).font(.system(size: 27, weight: .semibold))
                Text(subtitle).font(.system(size: 13)).foregroundStyle(.secondary)
            }
            Spacer()
            if model.busy { ProgressView().controlSize(.small).padding(.top, 8) }
        }.padding(.bottom, 2)
    }

    var subtitle: String {
        switch model.page {
        case .connection: return "Bring your Mistral access into Claude Desktop."
        case .models: return "Choose the Mistral model behind each Claude option."
        case .claude: return "Launch Claude with your Mistral configuration."
        case .activity: return "Requests through your local gateway."
        }
    }

    var connectionPage: some View {
        VStack(spacing: 18) {
            Panel {
                HStack(alignment: .top) {
                    Image(systemName: "key.horizontal").font(.system(size: 22)).foregroundStyle(bridgeOrange)
                    VStack(alignment: .leading, spacing: 6) { Text("Mistral account").font(.headline); Text(model.credentialFound ? "Connected through \(model.credentialSource)" : "Start Vibe and sign in to your Mistral account.").foregroundStyle(.secondary).font(.system(size: 12)) }
                    Spacer()
                    StatusPill(text: model.credentialFound ? "Connected" : "Setup needed", good: model.credentialFound)
                }
                Picker("Credentials", selection: $model.settings.credential_mode) {
                    Text("Use Vibe sign-in").tag("vibe"); Text("Separate API key").tag("separate")
                }.pickerStyle(.segmented)
                if model.settings.credential_mode == "separate" {
                    HStack { SecureField("Mistral API key", text: $model.secretDraft).textFieldStyle(.roundedBorder); Button("Save in Keychain") { Task { await model.saveKey() } }.disabled(model.busy || model.secretDraft.isEmpty) }
                    Text("Saved in macOS Keychain. The key stays out of configuration files and logs.").font(.caption).foregroundStyle(.secondary)
                } else {
                    HStack {
                        VStack(alignment: .leading, spacing: 4) { Text("Vibe’s selected model").font(.caption).foregroundStyle(.secondary); Text(model.vibeAlias.isEmpty ? "Waiting for Vibe" : model.vibeAlias).font(.system(size: 13, weight: .medium)) }
                        Spacer()
                        Button("Open Vibe") { model.openVibe() }
                        Button("Reconnect") { Task { await model.reconnect() } }.disabled(model.busy)
                    }
                }
            }
            Panel {
                HStack {
                    VStack(alignment: .leading, spacing: 6) { Text("Local gateway").font(.headline); Text(model.endpoint).font(.system(size: 12, design: .monospaced)).foregroundStyle(.secondary).textSelection(.enabled) }
                    Spacer()
                    Button(model.running ? "Stop gateway" : "Start gateway") { Task { await model.toggleGateway() } }.disabled(model.busy).controlSize(.large)
                }
                Divider()
                HStack { Text("Port").font(.system(size: 13)); Spacer(); TextField("11436", value: $model.settings.port, format: .number.grouping(.never)).textFieldStyle(.roundedBorder).frame(width: 90) }
                Text("The bridge runs on this Mac. Mistral performs inference in the cloud.").font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button("Test connection") { Task { await model.testRoute() } }.disabled(model.busy || !model.runtimeFound).controlSize(.large)
                Spacer()
                Button("Save settings") { Task { await model.saveFromUI() } }.disabled(model.busy || !model.changed)
                Button("Choose models →") { model.page = .models }.buttonStyle(.borderedProminent).controlSize(.large)
            }
        }
    }

    var modelsPage: some View {
        VStack(spacing: 18) {
            Panel {
                HStack {
                    VStack(alignment: .leading, spacing: 5) { Text("Model mappings").font(.headline); Text("Choose a model by name. Show technical IDs to enter a custom route.").font(.caption).foregroundStyle(.secondary) }
                    Spacer()
                    Button("Refresh models") { Task { await model.discover() } }.disabled(model.busy)
                }
                Text(model.catalogueSummary).font(.system(size: 11)).foregroundStyle(.secondary)
                ForEach(slots, id: \.id) { slot in
                    HStack(spacing: 10) {
                        VStack(alignment: .leading, spacing: 4) { Text(slot.label).font(.system(size: 12, weight: .medium)); if model.showRoutingIDs { Text(slot.id).font(.system(size: 8, design: .monospaced)).foregroundStyle(.tertiary) } }.frame(width: 100, alignment: .leading)
                        Image(systemName: "arrow.right").foregroundStyle(.tertiary)
                        VStack(alignment: .leading, spacing: 6) {
                            Menu {
                                ForEach(model.routeOptions, id: \.self) { identifier in
                                    Button("\(model.modelLabel(identifier)) — \(model.modelFacts(identifier))") { model.settings.mappings[slot.id] = identifier }.help(identifier)
                                }
                                Divider()
                                Button("Enter a custom model ID…") { model.showRoutingIDs = true }
                            } label: {
                                Text(model.modelLabel(model.settings.mappings[slot.id] ?? "")).font(.system(size: 12, weight: .medium)).lineLimit(1)
                            }.menuStyle(.borderlessButton).menuIndicator(.hidden)
                                .frame(maxWidth: .infinity, alignment: .leading).padding(10)
                                .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 7))
                                .help("API ID: \(model.settings.mappings[slot.id] ?? "")")
                            Text(model.modelFacts(model.settings.mappings[slot.id] ?? "")).font(.system(size: 9)).foregroundStyle(.secondary)
                            if model.showRoutingIDs {
                                TextField("Exact Mistral API model ID", text: Binding(get: { model.settings.mappings[slot.id] ?? "" }, set: { model.settings.mappings[slot.id] = $0 }))
                                    .textFieldStyle(.roundedBorder).font(.system(size: 10, design: .monospaced))
                            }
                        }.frame(maxWidth: .infinity)
                        Button("Test") { Task { await model.testRoute(slot.id) } }.disabled(model.busy)
                    }
                }
                Divider()
                HStack {
                    Toggle("Show technical IDs", isOn: $model.showRoutingIDs).toggleStyle(.checkbox).font(.caption)
                    Spacer()
                    Button("Use Vibe for all") { for slot in slots { model.settings.mappings[slot.id] = model.vibeModel } }
                }
            }
            Panel {
                Text("Model controls").font(.headline)
                HStack { Text("Context").font(.system(size: 13, weight: .medium)); Spacer(); Text("Reported by Mistral for each model").font(.caption).foregroundStyle(.secondary) }
                Divider()
                HStack { Text("Effort").font(.system(size: 13, weight: .medium)); Spacer(); Text("Use Claude’s Effort control").font(.caption).foregroundStyle(.secondary) }
                Text("For reasoning models, this follows Vibe’s current Mistral mapping: Low uses standard mode; Medium and above enable reasoning. Models without reasoning omit that parameter.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
                Divider()
                HStack { Text("Fast").font(.system(size: 13, weight: .medium)); Spacer(); Text("No same-model fast mode connected").font(.caption).foregroundStyle(.secondary) }
                Text("Vibe’s fast alias is Mistral Small. It appears once as that model, rather than a speed variant of Medium.").font(.caption).foregroundStyle(.secondary)
            }
            HStack { Spacer(); Button("Save mappings") { Task { await model.saveFromUI() } }.disabled(model.busy || !model.changed).controlSize(.large); Button("Launch Claude") { Task { await model.launchClaude() } }.buttonStyle(.borderedProminent).controlSize(.large).disabled(model.busy || !model.claudeInstalled) }
        }
    }

    var claudePage: some View {
        VStack(spacing: 18) {
            Panel {
                HStack(spacing: 18) {
                    Image(systemName: "macwindow").font(.system(size: 38, weight: .light)).foregroundStyle(bridgeOrange).frame(width: 60, height: 60)
                    VStack(alignment: .leading, spacing: 6) { Text("Claude, powered by Mistral").font(.system(size: 19, weight: .semibold)); Text("Your usual Claude interface with the models you choose.").font(.system(size: 12)).foregroundStyle(.secondary) }
                }
                Divider()
                infoRow("Mistral profile", model.profileActive ? "Active" : "Ready to launch", "person.crop.rectangle")
                infoRow("Conversations", "Saved by Claude’s third-party mode", "bubble.left.and.bubble.right")
                infoRow("Previous setup", "Restored after this Claude session closes", "arrow.uturn.backward")
                if model.claudeRunning && !model.profileActive {
                    Text("Claude is already open with another profile. Finish your current work and quit Claude before switching.").font(.system(size: 12)).foregroundStyle(.orange).padding(12).frame(maxWidth: .infinity, alignment: .leading).background(Color.orange.opacity(0.07), in: RoundedRectangle(cornerRadius: 8))
                }
                HStack {
                    Button(model.profileActive && model.claudeRunning ? "Show Claude" : "Launch Claude") { Task { await model.launchClaude() } }.buttonStyle(.borderedProminent).controlSize(.large).disabled(model.busy || !model.claudeInstalled)
                    if model.recoveryNeeded { Button("Restore previous setup") { Task { await model.restore() } }.disabled(model.busy || model.claudeRunning) }
                    Spacer()
                }
            }
            Panel {
                Toggle(isOn: $model.settings.auto_stop) { VStack(alignment: .leading, spacing: 5) { Text("Stop gateway when Claude quits").font(.system(size: 13, weight: .medium)); Text("The menu bar app stays available for your next session.").font(.caption).foregroundStyle(.secondary) } }.toggleStyle(.switch)
                Divider()
                Toggle(isOn: $model.settings.auto_mode) { VStack(alignment: .leading, spacing: 5) { Text("Enable Claude Auto mode").font(.system(size: 13, weight: .medium)); Text("Let Claude decide when to ask before making changes.").font(.caption).foregroundStyle(.secondary) } }.toggleStyle(.switch)
                Text("This uses Claude’s native third-party profile system, like Ollama. It switches the installed app’s profile; it does not create a simultaneous second Claude app. Existing Ollama and Claude conversations are retained.").font(.caption).foregroundStyle(.secondary).lineSpacing(3)
            }
            HStack { Spacer(); Button("Save preferences") { Task { await model.saveFromUI() } }.disabled(model.busy || !model.changed) }
        }
    }

    func infoRow(_ label: String, _ value: String, _ icon: String) -> some View {
        HStack { Image(systemName: icon).foregroundStyle(.secondary).frame(width: 20); Text(label).font(.system(size: 12, weight: .medium)); Spacer(); Text(value).font(.system(size: 12)).foregroundStyle(.secondary) }
    }

    var activityPage: some View {
        VStack(spacing: 18) {
            HStack(spacing: 12) { metric("Completed", model.completed); metric("In progress", model.activeRequests); metric("Tokens used", model.inputTokens + model.outputTokens) }
            Panel {
                HStack { Text("Recent requests").font(.headline); Spacer(); Text("Content stays out of logs").font(.caption).foregroundStyle(.secondary) }
                if model.activity.isEmpty {
                    VStack(spacing: 12) { Image(systemName: "waveform.path").font(.system(size: 35, weight: .light)).foregroundStyle(.tertiary); Text("Ready for your first request").font(.system(size: 14, weight: .medium)); Text("Test a model or launch Claude to see activity here.").font(.caption).foregroundStyle(.secondary) }.frame(maxWidth: .infinity).padding(.vertical, 44)
                } else {
                    ForEach(model.activity) { entry in
                        HStack(spacing: 10) {
                            Image(systemName: entry.event == "completed" ? "checkmark.circle.fill" : entry.event == "cancelled" ? "stop.circle" : "exclamationmark.circle").foregroundStyle(entry.event == "completed" ? Color.green : .orange)
                            VStack(alignment: .leading, spacing: 4) { Text(model.modelLabel(entry.model)).font(.system(size: 12, weight: .medium)).help(entry.model); Text(entry.event.capitalized + (entry.status.map { " · HTTP \($0)" } ?? "")).font(.caption).foregroundStyle(.secondary) }
                            Spacer(); Text(String(entry.time.dropFirst(11).prefix(8))).font(.system(size: 10, design: .monospaced)).foregroundStyle(.tertiary)
                        }
                    }
                }
            }
            HStack { Text("Counters cover this gateway run. Mistral provides the usage totals.").font(.caption).foregroundStyle(.secondary); Spacer(); Button("Open data folder") { NSWorkspace.shared.open(model.root) } }
        }
    }

    func metric(_ label: String, _ number: Int) -> some View {
        VStack(alignment: .leading, spacing: 9) { Text(label).font(.caption).foregroundStyle(.secondary); Text(number.formatted()).font(.system(size: 25, weight: .medium, design: .rounded)) }
            .padding(18).frame(maxWidth: .infinity, alignment: .leading).background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 12))
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, NSWindowDelegate {
    var model: BridgeModel!
    var statusItem: NSStatusItem!
    var window: NSWindow!
    var readyToQuit = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        NSApp.appearance = NSAppearance(named: .darkAqua)
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: "Mistral Bridge")
        let quitItem = NSMenuItem(title: "Quit Mistral Bridge", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        appMenu.addItem(quitItem); appItem.submenu = appMenu; mainMenu.addItem(appItem)
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        for (title, action, key) in [("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            editMenu.addItem(NSMenuItem(title: title, action: Selector(action), keyEquivalent: key))
        }
        editItem.submenu = editMenu; mainMenu.addItem(editItem); NSApp.mainMenu = mainMenu
        model = BridgeModel()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "point.3.connected.trianglepath.dotted", accessibilityDescription: "Mistral Bridge")
        statusItem.button?.image?.isTemplate = true
        statusItem.button?.toolTip = "Mistral Bridge"
        let menu = NSMenu(); menu.delegate = self; statusItem.menu = menu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 940, height: 740), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Mistral Bridge"
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentView = NSHostingView(rootView: BridgeWindow(model: model))
        window.center()
        showWindow()
    }

    @objc func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func launchClaude() { Task { await model.launchClaude(); if model.noticeIsError { showWindow() } } }
    @objc func toggleGateway() { Task { await model.toggleGateway() } }
    @objc func openVibe() { model.openVibe() }
    @objc func quit() { NSApp.terminate(nil) }
    @objc func restore() { Task { await model.restore() } }

    func menuWillOpen(_ menu: NSMenu) {
        menu.removeAllItems()
        let title = NSMenuItem(title: "Mistral Bridge · \(model.gatewayState)", action: nil, keyEquivalent: ""); title.isEnabled = false; menu.addItem(title)
        menu.addItem(.separator())
        add(menu, "Launch Claude…", #selector(launchClaude), "l")
        add(menu, "Models & Settings…", #selector(showWindow), ",")
        add(menu, "Open Mistral Vibe", #selector(openVibe), "")
        menu.addItem(.separator())
        add(menu, model.running ? "Stop Gateway" : "Start Gateway", #selector(toggleGateway), "")
        if model.recoveryNeeded { add(menu, "Restore Previous Claude Setup", #selector(restore), "") }
        menu.addItem(.separator())
        add(menu, "Quit Mistral Bridge", #selector(quit), "q")
    }

    func add(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key); item.target = self; menu.addItem(item)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if readyToQuit { return .terminateNow }
        model.updateClaudeRunning()
        if model.claudeRunning && model.profileActive {
            let alert = NSAlert()
            alert.messageText = "Claude is using Mistral Bridge"
            alert.informativeText = "Quit Claude first so your previous configuration can be restored and active work can finish."
            alert.addButton(withTitle: "Keep Bridge Open")
            alert.runModal()
            return .terminateCancel
        }
        if model.activeRequests > 0 || model.busy {
            model.tell("Wait for the current operation to finish before quitting.", error: true); showWindow(); return .terminateCancel
        }
        model.shuttingDown = true
        Task {
            if model.recoveryNeeded && !model.claudeRunning {
                do { _ = try await model.command("restore"); model.recoveryNeeded = false }
                catch { model.shuttingDown = false; model.tell(error.localizedDescription, error: true); showWindow(); NSApp.reply(toApplicationShouldTerminate: false); return }
            }
            await model.stopGateway()
            readyToQuit = true
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}

@main
struct MistralBridgeMain {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }
}
