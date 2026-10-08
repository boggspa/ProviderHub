import AppKit
import SwiftUI

struct ChatTextStyle {
    var size: Double = 13
    var monospaced = false
    var font: Font { .system(size: size, design: monospaced ? .monospaced : .default) }
    var editorFont: NSFont { monospaced ? .monospacedSystemFont(ofSize: size + 1, weight: .regular) : .systemFont(ofSize: size + 1) }
}
private struct ChatTextStyleKey: EnvironmentKey { static let defaultValue = ChatTextStyle() }
extension EnvironmentValues {
    var chatTextStyle: ChatTextStyle {
        get { self[ChatTextStyleKey.self] }
        set { self[ChatTextStyleKey.self] = newValue }
    }
}

struct ChatSettingsMenu: View {
    @ObservedObject var model: ChatModel
    @AppStorage(HubTheme.Appearance.defaultsKey) private var appearance = HubTheme.Appearance.Mode.system
    @AppStorage(HubTheme.WindowStyle.defaultsKey) private var surface = HubTheme.WindowStyle.Mode.glass
    @AppStorage("chatMonospacedText") private var monospaced = false
    @AppStorage("chatTextSize") private var textSize = 13.0
    var body: some View {
        Menu {
            Menu("Theme") {
                ForEach(HubTheme.Appearance.Mode.allCases, id: \.self) { mode in
                    Button { appearance = mode; HubTheme.Appearance.apply() } label: {
                        if appearance == mode { Label(mode.title, systemImage: "checkmark") } else { Text(mode.title) }
                    }
                }
            }
            Menu("Window") {
                ForEach(HubTheme.WindowStyle.Mode.allCases, id: \.self) { mode in
                    Button { surface = mode; model.onSurfaceChange?() } label: {
                        if surface == mode { Label(mode.title, systemImage: "checkmark") } else { Text(mode.title) }
                    }
                }
            }
            Divider()
            Menu("Font") {
                Button { monospaced = false } label: { if !monospaced { Label("System", systemImage: "checkmark") } else { Text("System") } }
                Button { monospaced = true } label: { if monospaced { Label("Monospaced", systemImage: "checkmark") } else { Text("Monospaced") } }
            }
            Menu("Text size") {
                ForEach([12.0, 13.0, 15.0], id: \.self) { size in
                    Button { textSize = size } label: {
                        let name = size == 12 ? "Small" : size == 13 ? "Default" : "Large"
                        if textSize == size { Label(name, systemImage: "checkmark") } else { Text(name) }
                    }
                }
            }
        } label: {
            Image(systemName: "gearshape").font(.system(size: 13)).foregroundStyle(.secondary)
        }.menuStyle(.borderlessButton).fixedSize().accessibilityLabel("Chat settings")
    }
}

struct ChatGitIndicator: View {
    @ObservedObject var model: ChatModel
    var body: some View {
        Group {
            if let git = model.gitStatus {
                values(git)
            }
        }
        .task(id: model.selected?.workspace) {
            model.refreshGitStatus()
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 4_000_000_000) } catch { return }
                if NSApp.isActive { model.refreshGitStatus() }
            }
        }
    }
    private func values(_ git: ChatGitStatus) -> some View {
        HStack(spacing: 4) {
            Text("\(git.files) \(git.files == 1 ? "file" : "files")").foregroundStyle(.secondary)
            separator
            Text("+\(git.added)").foregroundStyle(Color(nsColor: .systemGreen))
            Text("−\(git.deleted)").foregroundStyle(Color(nsColor: .systemRed))
            if git.ahead > 0 || (git.behind ?? 0) > 0 { separator }
            if git.ahead > 0 { Text("↑\(git.ahead)").foregroundStyle(aheadGold) }
            if let behind = git.behind, behind > 0 { Text("↓\(behind)").foregroundStyle(Color(nsColor: .systemBlue)) }
        }.font(.system(size: 11.5, weight: .medium, design: .monospaced)).fixedSize()
            .help(explanation(git)).accessibilityElement(children: .combine)
    }
    private var aheadGold: Color {
        HubTheme.Semantic.dynamic(light: NSColor(red: 0.64, green: 0.43, blue: 0.04, alpha: 1),
                                  dark: NSColor(red: 1, green: 0.77, blue: 0.28, alpha: 1))
    }
    private func explanation(_ git: ChatGitStatus) -> String {
        let comparison = git.upstream.map { "Ahead/behind cached " + $0 + "; tracked diff totals" }
            ?? "↑ counts local commits; no upstream; tracked diff totals"
        return git.branch + " · " + comparison
    }
    private var separator: some View { Text("|").foregroundStyle(.tertiary) }
}
