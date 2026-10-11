import AppKit
import SwiftUI

/// Optional capabilities use the same quiet native rows as Chat settings.
struct ChatCapabilitiesSettings: View {
    @ObservedObject var model: ChatModel
    @Environment(\.dismiss) private var dismiss
    private var computer: [String: Any]? { model.pluginStatus.first { $0["id"] as? String == "peekaboo" } }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack { Text("Plugins").font(.headline); Spacer(); Button("Done") { dismiss() }.keyboardShortcut(.cancelAction) }
            Form {
                Section("Browser Use") {
                    Toggle("Allow agents to use the browser", isOn: Binding(get: { model.browserUseEnabled }, set: model.setBrowserUseEnabled))
                    Text("Built in · WebKit. Agents operate the same page you see. Actions follow this chat’s approval mode.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Computer Use") {
                    Toggle("Enable Computer Use", isOn: Binding(get: { computer?["enabled"] as? Bool ?? false }, set: model.setComputerUseEnabled))
                        .disabled(computer == nil)
                    HStack {
                        Text(computerStatus).font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        if model.pluginStarting { ProgressView().controlSize(.small) }
                        else if computer?["enabled"] as? Bool == true && computer?["running"] as? Bool != true {
                            Button("Connect") { model.startComputerUse() }
                        }
                    }
                    Button(computer == nil ? "Choose Peekaboo executable…" : "Change executable…") { chooseExecutable() }
                    DisclosureGroup("Dependency and setup") {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(dependencyDescription)
                            Text("Screen Recording and Accessibility permissions are managed by macOS. Enabling this plugin connects its MCP server; each tool still follows your chat’s approval mode.")
                            if let raw = model.computerUseDependency["release"] as? String, let url = URL(string: raw), url.scheme == "https" {
                                Link("Open pinned release", destination: url)
                            }
                            if let executable = computer?["executable"] as? String {
                                Text(executable).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                            }
                        }.font(.caption).foregroundStyle(.secondary)
                    }
                }
            }.formStyle(.grouped)
            if !model.pluginNotice.isEmpty { Text(model.pluginNotice).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
        }.padding(20).frame(width: 480, height: 440)
    }

    private var computerStatus: String {
        if #available(macOS 15, *) {
            guard let computer else { return "Optional dependency not configured" }
            if computer["running"] as? Bool == true { return "Connected · version checked" }
            switch computer["availability"] as? String {
            case "available": return "Installed · not connected"
            case "missing_executable": return "Executable no longer found"
            case "executable_path_changed", "executable_hash_mismatch": return "Executable changed · select it again"
            case "unsupported_macos", "requires_macos": return "Requires macOS 15 or later"
            default: return "Unavailable · review setup"
            }
        }
        return "Requires macOS 15 or later"
    }

    private var dependencyDescription: String {
        let version = model.computerUseDependency["version"] as? String ?? ""
        let license = model.computerUseDependency["license"] as? String ?? ""
        let minimum = model.computerUseDependency["minimum_macos"] as? String ?? "15.0"
        return "Peekaboo \(version) · \(license) · macOS \(minimum)+. The native executable needs no Node runtime. Install the pinned release, then select its executable here."
    }

    private func chooseExecutable() {
        let picker = NSOpenPanel()
        picker.canChooseDirectories = false; picker.canChooseFiles = true; picker.allowsMultipleSelection = false
        picker.prompt = "Use executable"
        if picker.runModal() == .OK, let url = picker.url { model.configureComputerUse(url.path) }
    }
}
